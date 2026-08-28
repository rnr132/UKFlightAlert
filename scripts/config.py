"""Shared config/path loading for the sweep scripts.

Split out once a second script (storage.py) needed the same load_config() —
duplicating five lines across scripts felt worse than one shared import.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "sweep.yaml"


def load_config(path=CONFIG_PATH):
    with open(path, "r") as f:
        return yaml.safe_load(f)
