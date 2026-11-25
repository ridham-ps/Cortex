"""Team annotations — short human notes about a file ("XfrmSvc converts legacy
orders to v2"), saved per repo in ns_dir/annotations.json.

Saved through `POST /v1/annotations/{ns}` and shown on the matching guided-tour
stop as its `note`.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from cortex.config import Settings, get_settings
from cortex.index.store import get_store, slugify


def _annot_path(store, namespace: str):
    return store.ns_dir(slugify(namespace)) / "annotations.json"


def load_annotations(namespace: str, *, settings: Optional[Settings] = None) -> list[dict]:
    store = get_store(settings or get_settings())
    p = _annot_path(store, namespace)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    except (OSError, json.JSONDecodeError):
        return []


def save_annotation(namespace: str, file: str, answer: str, *,
                    symbol: str = "", settings: Optional[Settings] = None) -> list[dict]:
    """Record (or update) the human's explanation of a file/symbol."""
    store = get_store(settings or get_settings())
    items = load_annotations(namespace, settings=settings)
    answer = (answer or "").strip()
    items = [a for a in items if not (a.get("file") == file and a.get("symbol", "") == symbol)]
    if answer:
        items.append({"file": file, "symbol": symbol, "answer": answer[:1500], "ts": time.time()})
    p = _annot_path(store, namespace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return items
