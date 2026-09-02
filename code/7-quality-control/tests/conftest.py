import sys
from pathlib import Path

QC_DIRECTORY = Path(__file__).resolve().parents[1]
STANDARDIZATION_DIRECTORY = QC_DIRECTORY.parent / "4-standardization"
sys.path.insert(0, str(QC_DIRECTORY))
sys.path.insert(0, str(STANDARDIZATION_DIRECTORY))
