"""Make script-style standardization modules importable during tests."""

import sys
from pathlib import Path

STAGE_DIRECTORY = Path(__file__).resolve().parent
if str(STAGE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(STAGE_DIRECTORY))
