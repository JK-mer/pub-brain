"""Filesystem locations. Overridable so the tree stays portable."""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("PUBBRAIN_DATA_DIR", _REPO_ROOT / "data"))
DB_PATH = Path(os.environ.get("PUBBRAIN_DB", DATA_DIR / "catalog.db"))
RAW_DIR = DATA_DIR / "raw"
PDF_DIR = DATA_DIR / "pdf"


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
