#!/usr/bin/env python3
"""
tasks.py — cross-platform dev task runner for m3 (Windows-friendly alternative to make).

Usage:
    python tasks.py <task>

Tasks:
    setup           Create venv and install all dependencies
    test            Run the test suite
    test-cov        Run tests with coverage report
    lint            Check code with ruff
    format          Auto-fix lint issues with ruff
    typecheck       Run mypy type checks
    dev             Start the app (scheduler + web dashboard)
    dev-reload      Start with DEBUG=true (uvicorn auto-reload)
    once            Run one metadata pass and exit
    validate        Validate all plugins in ./plugins/
    gen-test-lib    Generate a fake media library in ./test-media/
    clean           Remove venv and caches
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VENV = ROOT / ".venv"

# Platform-aware bin path
if sys.platform == "win32":
    BIN = VENV / "Scripts"
    PY_EXE = "python"
else:
    BIN = VENV / "bin"
    PY_EXE = "python3"

PY  = str(BIN / PY_EXE) if VENV.exists() else PY_EXE
PIP = str(BIN / "pip")


def run(*cmd: str, env: dict | None = None, check: bool = True) -> int:
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, env=merged_env)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result.returncode


TASKS = {}

def task(name):
    def decorator(fn):
        TASKS[name] = fn
        return fn
    return decorator


@task("setup")
def setup():
    """Create venv and install all dependencies."""
    run(PY_EXE, "-m", "venv", str(VENV))
    run(PIP, "install", "--upgrade", "pip")
    run(PIP, "install", "-r", "requirements.txt", "-r", "requirements-dev.txt")
    print("\nSetup complete. Copy .env.example to .env and fill in your values.")
    if sys.platform == "win32":
        print(f"Activate: {VENV}\\Scripts\\activate.bat  (CMD)")
        print(f"          {VENV}\\Scripts\\Activate.ps1   (PowerShell)")
    else:
        print(f"Activate: source {VENV}/bin/activate")


@task("test")
def test():
    """Run the test suite."""
    run(PY, "-m", "pytest", "app/tests/", "-v")


@task("test-cov")
def test_cov():
    """Run tests with coverage report."""
    run(PY, "-m", "pytest", "app/tests/", "-v",
        "--cov=app", "--cov-report=term-missing")


@task("lint")
def lint():
    """Check code with ruff."""
    run(PY, "-m", "ruff", "check", "app/")


@task("format")
def fmt():
    """Auto-fix lint issues with ruff."""
    run(PY, "-m", "ruff", "check", "--fix", "app/")


@task("typecheck")
def typecheck():
    """Run mypy type checks."""
    run(PY, "-m", "mypy", "app/", "--ignore-missing-imports")


@task("dev")
def dev():
    """Start the app (scheduler + web dashboard)."""
    run(PY, "-m", "app.main")


@task("dev-reload")
def dev_reload():
    """Start with DEBUG=true (uvicorn auto-reload)."""
    run(PY, "-m", "app.main", env={"DEBUG": "true"})


@task("once")
def once():
    """Run one metadata pass and exit."""
    run(PY, "-m", "app.main", "--once")


@task("validate")
def validate():
    """Validate all plugins in ./plugins/."""
    run(PY, "-m", "app.main", "--validate-plugins")


@task("gen-test-lib")
def gen_test_lib():
    """Generate a fake media library in ./test-media/."""
    run(PY, "scripts/generate_test_library.py")


@task("clean")
def clean():
    """Remove venv and caches."""
    for path in [VENV, ROOT / ".coverage", ROOT / "coverage.xml"]:
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for pycache in ROOT.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    for pyc in ROOT.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)
    print("Cleaned.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        print("Available tasks:")
        for name, fn in TASKS.items():
            print(f"  {name:<16} {fn.__doc__ or ''}")
        return

    task_name = sys.argv[1]
    if task_name not in TASKS:
        print(f"Unknown task: {task_name!r}")
        print(f"Available: {', '.join(TASKS)}")
        sys.exit(1)

    TASKS[task_name]()


if __name__ == "__main__":
    main()
