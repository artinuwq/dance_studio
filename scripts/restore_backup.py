import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MEDIA_TARGET = ROOT / "var" / "media"
DEFAULT_AGE_KEY_REL = Path("scripts") / "keys" / "backup-prod.agekey"
DEFAULT_BACKUP_DIR_REL = Path("scripts") / "backup"
DEFAULT_AGE_BIN_REL = Path("scripts") / "tools" / ("age.exe" if os.name == "nt" else "age")


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_project_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (ROOT / path).resolve()


def _resolve_age_binary_path(raw_path: str) -> str:
    configured = str(raw_path or "").strip()
    if configured:
        configured_path = _resolve_project_path(configured)
        if configured_path.exists():
            return str(configured_path)
        raise RuntimeError(f"Configured age binary not found: {configured_path}")

    default_local = _resolve_project_path(DEFAULT_AGE_BIN_REL)
    if default_local.exists():
        return str(default_local)

    for binary_name in ("age", "age.exe"):
        resolved = shutil.which(binary_name)
        if resolved:
            return resolved

    raise RuntimeError(
        "age binary not found. Set BACKUP_AGE_BINARY in .env or place binary at scripts/tools/age(.exe)"
    )


def _resolve_pg_restore_command(database_url: str, pg_restore_path: str, dump_path: Path) -> tuple[list[str], dict[str, str]]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError(f"Unsupported DB for pg_restore: {url.drivername}")
    if not url.database:
        raise RuntimeError("DATABASE_URL has no database name")

    cmd = [
        pg_restore_path,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        str(url.database),
    ]
    if url.host:
        cmd.extend(["--host", str(url.host)])
    if url.port:
        cmd.extend(["--port", str(url.port)])
    if url.username:
        cmd.extend(["--username", str(url.username)])
    sslmode = (url.query or {}).get("sslmode")
    if sslmode:
        cmd.extend(["--sslmode", str(sslmode)])
    cmd.append(str(dump_path))

    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return cmd, env


def _run_command(cmd: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"Command failed: {' '.join(cmd)}")


def _decrypt_age_file(age_path: str, key_path: Path, encrypted_path: Path, output_path: Path) -> None:
    cmd = [
        age_path,
        "--decrypt",
        "--identity",
        str(key_path),
        "--output",
        str(output_path),
        str(encrypted_path),
    ]
    _run_command(cmd)


def _clear_directory_contents(target: Path) -> None:
    if not target.exists():
        return
    for child in target.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_resolved = target_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            destination = (target_dir / member.filename).resolve()
            if not str(destination).startswith(str(target_resolved)):
                raise RuntimeError(f"Unsafe path in media archive: {member.filename}")
        zf.extractall(target_dir)


def _decrypted_name(encrypted_path: Path, fallback_suffix: str) -> str:
    if encrypted_path.name.endswith(".age"):
        return encrypted_path.name[:-4]
    return f"{encrypted_path.name}{fallback_suffix}"


def _latest_backup_file(backup_dir: Path, patterns: list[str]) -> Path | None:
    if not backup_dir.exists():
        return None
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend([p for p in backup_dir.glob(pattern) if p.is_file()])
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore encrypted DB/media backups in one command."
    )
    parser.add_argument(
        "--age-key",
        default=str(DEFAULT_AGE_KEY_REL),
        help="Path to private age key file. Default: scripts/keys/backup-prod.agekey",
    )
    parser.add_argument(
        "--age-bin",
        default=os.getenv("BACKUP_AGE_BINARY", ""),
        help="Path to age binary (optional). Defaults: .env BACKUP_AGE_BINARY, then scripts/tools/age(.exe), then PATH.",
    )
    parser.add_argument(
        "--encrypted-db",
        help="Path to encrypted DB dump (.dump.age).",
    )
    parser.add_argument(
        "--encrypted-media",
        help="Path to encrypted media archive (.zip.age).",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(DEFAULT_BACKUP_DIR_REL),
        help="Directory to auto-discover backups when encrypted paths are omitted. Default: scripts/backup",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="SQLAlchemy DATABASE_URL. Defaults to DATABASE_URL from environment/.env.",
    )
    parser.add_argument(
        "--media-target",
        default=str(DEFAULT_MEDIA_TARGET),
        help=f"Target directory for media restore. Default: {DEFAULT_MEDIA_TARGET}",
    )
    parser.add_argument(
        "--clear-media-target",
        action="store_true",
        help="Delete existing files in media-target before extraction.",
    )
    parser.add_argument(
        "--keep-decrypted",
        action="store_true",
        help="Keep decrypted intermediate files on disk after restore.",
    )
    return parser.parse_args()


def main() -> int:
    _load_dotenv(ROOT / ".env")
    args = parse_args()

    backup_dir = _resolve_project_path(args.backup_dir)
    encrypted_db_path = _resolve_project_path(args.encrypted_db) if args.encrypted_db else _latest_backup_file(
        backup_dir,
        ["db_backup_*.dump.age", "*.dump.age"],
    )
    encrypted_media_path = _resolve_project_path(args.encrypted_media) if args.encrypted_media else _latest_backup_file(
        backup_dir,
        ["media_backup_*.zip.age", "*.zip.age"],
    )
    key_path = _resolve_project_path(args.age_key)
    media_target = _resolve_project_path(args.media_target)
    database_url = (args.database_url or "").strip()

    if not encrypted_db_path and not encrypted_media_path:
        print(
            f"No encrypted files found. Pass --encrypted-db/--encrypted-media or place files in {backup_dir}.",
            file=sys.stderr,
        )
        return 1
    if not key_path.exists():
        print(
            f"Age key file not found: {key_path}\n"
            f"Put key into {SCRIPT_DIR / 'keys'} or pass --age-key explicitly.",
            file=sys.stderr,
        )
        return 1
    if encrypted_db_path and not database_url:
        print("DATABASE_URL is required when --encrypted-db is provided.", file=sys.stderr)
        return 1
    if encrypted_db_path and not encrypted_db_path.exists():
        print(f"Encrypted DB dump not found: {encrypted_db_path}", file=sys.stderr)
        return 1
    if encrypted_media_path and not encrypted_media_path.exists():
        print(f"Encrypted media archive not found: {encrypted_media_path}", file=sys.stderr)
        return 1

    try:
        age_path = _resolve_age_binary_path(args.age_bin)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    pg_restore_path = None
    if encrypted_db_path:
        pg_restore_path = shutil.which("pg_restore")
        if not pg_restore_path:
            print("pg_restore not found in PATH.", file=sys.stderr)
            return 1

    temp_dir = None
    try:
        if not args.keep_decrypted:
            temp_dir = tempfile.TemporaryDirectory(prefix="dance_restore_all_")
            staging_dir = Path(temp_dir.name)
        else:
            staging_dir = (encrypted_db_path or encrypted_media_path).parent

        if encrypted_db_path:
            decrypted_db = staging_dir / _decrypted_name(encrypted_db_path, ".dump")
            _decrypt_age_file(age_path, key_path, encrypted_db_path, decrypted_db)
            restore_cmd, restore_env = _resolve_pg_restore_command(
                database_url=database_url,
                pg_restore_path=pg_restore_path,
                dump_path=decrypted_db,
            )
            _run_command(restore_cmd, env=restore_env)

        if encrypted_media_path:
            media_target.mkdir(parents=True, exist_ok=True)
            if args.clear_media_target:
                _clear_directory_contents(media_target)
            decrypted_media = staging_dir / _decrypted_name(encrypted_media_path, ".zip")
            _decrypt_age_file(age_path, key_path, encrypted_media_path, decrypted_media)
            _safe_extract_zip(decrypted_media, media_target)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("Restore completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
