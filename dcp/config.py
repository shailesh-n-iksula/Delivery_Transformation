from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "config" / "rules.json"


def load_rules(path=None) -> dict:
    """Rules come from --rules, then $DCP_RULES, then config/rules.json."""
    p = Path(path or os.environ.get("DCP_RULES") or DEFAULT_RULES)
    with open(p, encoding="utf-8") as f:
        return json.load(f)
