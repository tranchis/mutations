"""Minimal prompt catalog for the local generation demo."""

import json
from functools import lru_cache
from pathlib import Path

BUNDLE = Path(__file__).with_name("demo")


@lru_cache(maxsize=1)
def catalog():
    return json.loads((BUNDLE / "catalog.json").read_text())
