import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never touch the developer's real agentcraft.db — they create, roll back
# and delete projects, and those rows used to show up in the UI's session list.
# Set before any app import so get_settings() picks it up.
_TEST_DB = Path(tempfile.gettempdir()) / "agentcraft_tests.db"
_TEST_DB.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
