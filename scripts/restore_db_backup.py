import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
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
        description="Decrypt age-encrypted DB backup (.dump.age) and restore PostgreSQL database."
    )
    parser.add_argument(
        "--encrypted-dump",
        help="Path to encrypted backup file (.age). If omitted, latest file is auto-picked from --backup-dir.",
    )
    parser.add_argument(
        "--backup-dir",
        default=str(DEFAULT_BACKUP_DIR_REL),
        help="Directory to auto-discover DB backups when --encrypted-dump is omitted. Default: scripts/backup",
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
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="SQLAlchemy DATABASE_URL. Defaults to DATABASE_URL from environment/.env.",
    )
    parser.add_argument(
        "--keep-decrypted",
        action="store_true",
        help="Keep decrypted dump file on disk after restore.",
    )
    return parser.parse_args()


def main() -> int:
    _load_dotenv(ROOT / ".env")
    args = parse_args()

    backup_dir = _resolve_project_path(args.backup_dir)
    encrypted_dump_path = _resolve_project_path(args.encrypted_dump) if args.encrypted_dump else _latest_backup_file(
        backup_dir,
        ["db_backup_*.dump.age", "*.dump.age"],
    )
    age_key_path = _resolve_project_path(args.age_key)
    database_url = (args.database_url or "").strip()

    if not encrypted_dump_path:
        print(
            f"No encrypted DB dump found. Pass --encrypted-dump or place file in {backup_dir}.",
            file=sys.stderr,
        )
        return 1
    if not encrypted_dump_path.exists():
        print(f"Encrypted dump file not found: {encrypted_dump_path}", file=sys.stderr)
        return 1
    if not age_key_path.exists():
        print(
            f"Age key file not found: {age_key_path}\n"
            f"Put key into {SCRIPT_DIR / 'keys'} or pass --age-key explicitly.",
            file=sys.stderr,
        )
        return 1
    if not database_url:
        print("DATABASE_URL is required (pass --database-url or set it in .env).", file=sys.stderr)
        return 1

    try:
        age_path = _resolve_age_binary_path(args.age_bin)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    pg_restore_path = shutil.which("pg_restore")
    if not pg_restore_path:
        print("pg_restore not found in PATH.", file=sys.stderr)
        return 1

    decrypted_filename = encrypted_dump_path.name[:-4] if encrypted_dump_path.name.endswith(".age") else f"{encrypted_dump_path.name}.dump"
    decrypted_dump_path: Path | None = None
    temp_dir: tempfile.TemporaryDirectory | None = None

    try:
        if args.keep_decrypted:
            decrypted_dump_path = encrypted_dump_path.with_name(decrypted_filename)
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix="dance_restore_")
            decrypted_dump_path = Path(temp_dir.name) / decrypted_filename

        decrypt_cmd = [
            age_path,
            "--decrypt",
            "--identity",
            str(age_key_path),
            "--output",
            str(decrypted_dump_path),
            str(encrypted_dump_path),
        ]
        decrypt_result = subprocess.run(
            decrypt_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if decrypt_result.returncode != 0:
            stderr = (decrypt_result.stderr or "").strip()
            raise RuntimeError(f"age decrypt failed: {stderr or 'unknown error'}")

        restore_cmd, restore_env = _resolve_pg_restore_command(
            database_url=database_url,
            pg_restore_path=pg_restore_path,
            dump_path=decrypted_dump_path,
        )
        restore_result = subprocess.run(
            restore_cmd,
            env=restore_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if restore_result.returncode != 0:
            stderr = (restore_result.stderr or "").strip()
            raise RuntimeError(f"pg_restore failed: {stderr or 'unknown error'}")
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
