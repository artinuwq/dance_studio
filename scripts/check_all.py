from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> None:
    print(f"[check_all] {' '.join(command)}")
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> None:
    _run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"])
    _run([sys.executable, "-m", "compileall", "src", "scripts", "tests"])
    _run([sys.executable, "-m", "pytest", "-q"])


if __name__ == "__main__":
    main()
