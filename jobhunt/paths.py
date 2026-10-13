"""Where things live on disk.

Everything personal sits under one `data/` folder next to the app (gitignored).
Tests point JOBHUNT_DATA_DIR at a temporary folder instead.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "documents"


def data_dir() -> Path:
    d = Path(os.environ.get("JOBHUNT_DATA_DIR") or (APP_DIR / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_dir() -> Path:
    d = data_dir() / "jobs"
    d.mkdir(exist_ok=True)
    return d


def cv_dir() -> Path:
    d = data_dir() / "cv"
    d.mkdir(exist_ok=True)
    return d


def browser_profile_dir() -> Path:
    d = data_dir() / "browser-profile"
    d.mkdir(exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "jobhunt.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def profile_path() -> Path:
    return data_dir() / "profile.md"


def scope_path() -> Path:
    return data_dir() / "scope.md"
