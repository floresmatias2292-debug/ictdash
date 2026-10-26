"""Cross-platform launcher: python3 start.py  (Linux/macOS)  |  py start.py  (Windows)

Creates .venv if it is missing or was copied from another computer, installs requirements.txt
when it changes, then starts the dashboard on http://127.0.0.1:8050 (PORT env var overrides).
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"
WIN = os.name == "nt"
VPY = VENV / ("Scripts/python.exe" if WIN else "bin/python")


def fail(msg: str) -> None:
    print(f"\n[ictdash] ERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def venv_ok() -> bool:
    # A venv copied from another machine points at a Python that does not exist here.
    if not VPY.exists():
        return False
    try:
        out = subprocess.run([str(VPY), "-c", "import sys; print(sys.prefix)"],
                             capture_output=True, text=True, timeout=30)
    except OSError:
        return False
    return out.returncode == 0 and Path(out.stdout.strip()).resolve() == VENV.resolve()


def main() -> None:
    os.chdir(ROOT)
    if sys.version_info < (3, 12):
        fail(f"Python 3.12 or newer is required (numpy 2.5 / pandas 3.1); this is {sys.version.split()[0]}.\n"
             "  Ubuntu/Debian: sudo apt install python3.12 python3.12-venv  then  python3.12 start.py\n"
             "  Windows: install from https://www.python.org/downloads/ (tick 'Add to PATH'), then  py -3.12 start.py")

    if not venv_ok():
        if VENV.exists():
            print("[ictdash] .venv is broken or from another computer -> recreating it")
            shutil.rmtree(VENV)
        print("[ictdash] creating .venv ...")
        r = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
        if r.returncode != 0 or not VPY.exists():
            fail("could not create the virtual environment.\n"
                 "  Ubuntu/Debian: sudo apt install python3-venv  (or python3.12-venv), then run again.")

    want = hashlib.sha256(REQ.read_bytes()).hexdigest()
    if not STAMP.exists() or STAMP.read_text().strip() != want:
        print("[ictdash] installing dependencies (first run takes a minute) ...")
        subprocess.run([str(VPY), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        r = subprocess.run([str(VPY), "-m", "pip", "install", "-r", str(REQ)])
        if r.returncode != 0:
            fail("pip install failed (see the messages above). Check the internet connection and Python version.")
        STAMP.write_text(want)

    port = os.environ.get("PORT", "8050")
    print(f"[ictdash] starting on http://127.0.0.1:{port}")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [str(VPY), "-W", "ignore", "-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", port]
    if WIN:
        sys.exit(subprocess.call(cmd))
    os.execv(str(VPY), cmd)


if __name__ == "__main__":
    main()
