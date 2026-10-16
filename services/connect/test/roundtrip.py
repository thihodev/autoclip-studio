"""AutoClip opens what the Worker seals: python test/roundtrip.py (from services/connect, with the app's venv)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent))
from app import connect  # noqa: E402

key, public = connect.new_key()
out = Path(tempfile.mkdtemp()) / "sealed.txt"
subprocess.run(["node", "--test", "--test-name-pattern=callback", str(HERE / "flow.test.mjs")], check=True,
               env={**os.environ, "TEST_PUBLIC_KEY": public, "SEALED_OUT": str(out)}, capture_output=True)
tok = connect.open_sealed(out.read_text(), key)
assert tok["access_token"] == "act.1" and tok["refresh_token"] == "rft.1", tok
other, _ = connect.new_key()
try:
    connect.open_sealed(out.read_text(), other)
    raise SystemExit("another key must not open it")
except Exception as e:
    if isinstance(e, SystemExit):
        raise
print("ok: sealed for this AutoClip only", sorted(tok))
