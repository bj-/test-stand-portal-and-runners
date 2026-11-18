"""Read-only Docker/Compose probe with bounded process time; no environment output."""
import sys
sys.dont_write_bytecode = True

import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
from portal_local import LocalError, check_docker

try:
    check_docker(dict(os.environ))
except (LocalError, OSError, UnicodeError):
    raise SystemExit(1)
