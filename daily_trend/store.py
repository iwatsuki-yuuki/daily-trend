"""日ごとの結果の保存と読み出し。"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

from .common import CANDIDATES, DAYS

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def list_days() -> list[str]:
    if not DAYS.exists():
        return []
    return sorted(p.stem for p in DAYS.iterdir() if _DAY_RE.match(p.name))


def load_day(day: str) -> dict | None:
    p = DAYS / f"{day}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def latest() -> dict | None:
    days = list_days()
    return load_day(days[-1]) if days else None


def save_day(record: dict, markdown: str) -> Path:
    p = DAYS / f"{record['date']}.json"
    _atomic_write(p, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(DAYS / f"{record['date']}.md", markdown)
    return p


def save_candidates(day: str, payload: dict) -> None:
    _atomic_write(CANDIDATES / f"{day}.json", json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def shown(rec: dict | None) -> list[dict]:
    """その日に出した記事。同じ日に作り直したときは、置き換える前に出していた記事（replaced）も含む。"""
    if not rec:
        return []
    src = rec.get("source") or {}
    return [*(rec.get("replaced") or []), {"url": src.get("url"), "title": src.get("title")}]


def history(today: str, days_back: int) -> list[dict]:
    """過去 days_back 日に出した記事（重複除外用）。"""
    start = date.fromisoformat(today) - timedelta(days=days_back)
    out = []
    for d in list_days():
        if not (start.isoformat() <= d < today):
            continue
        out.extend(shown(load_day(d)))
    return out
