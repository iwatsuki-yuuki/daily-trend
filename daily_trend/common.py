"""共通: パス、設定、HTTP、候補データ型。"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("DAILY_TREND_HOME", Path.home() / "daily-trend"))
DATA = ROOT / "data"
DAYS = DATA / "days"
CANDIDATES = DATA / "candidates"
LOGS = DATA / "logs"

USER_AGENT = "daily-trend/0.1 (personal daily digest; 1 request per source per day)"


def load_config() -> dict:
    path = ROOT / "config.toml"
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def now_local() -> datetime:
    return datetime.now().astimezone()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def http_get_bytes(url: str, *, timeout: float = 20, headers: dict | None = None, retries: int = 2) -> tuple[bytes, str | None]:
    """GET して (本文のバイト列, Content-Type の charset) を返す。429/5xx/通信エラーは短い間隔で再試行する。"""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw, resp.headers.get_content_charset()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


def http_get(url: str, **kw) -> str:
    raw, charset = http_get_bytes(url, **kw)
    return raw.decode(charset or "utf-8", errors="replace")


def to_local_date(iso: str | None) -> str | None:
    """UTC などの ISO 時刻をローカル日付にする（UTC のまま [:10] で切ると日本時間 0〜9 時の分が前日になる）。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10] or None


# ---------- robots.txt（RFC 9309。* と $ を扱い、最長一致・同長なら Allow 優先）----------
_ROBOTS: dict[str, list[tuple[bool, str]] | None] = {}
_AGENT_TOKEN = "daily-trend"


def _robots_rules(origin: str) -> list[tuple[bool, str]] | None:
    """適用されるルールの一覧。None は「全面拒否とみなす」（robots.txt が 5xx・通信不能）。"""
    if origin in _ROBOTS:
        return _ROBOTS[origin]
    try:
        text = http_get(origin + "/robots.txt", timeout=10, retries=1)
    except urllib.error.HTTPError as e:
        rules = [] if 400 <= e.code < 500 else None  # 4xx: 制限なし / 5xx: 全面拒否（RFC 9309 2.3.1）
        _ROBOTS[origin] = rules
        return rules
    except Exception:
        _ROBOTS[origin] = None
        return None
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules_cur: list[tuple[bool, str]] | None = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules_cur is not None:  # 前のグループが閉じた
                agents, rules_cur = [], None
            agents.append(val.lower())
        elif key in ("allow", "disallow"):
            if rules_cur is None:
                rules_cur = []
                groups.append((agents, rules_cur))
            if val:
                rules_cur.append((key == "allow", val))
    mine = [r for ags, rs in groups if _AGENT_TOKEN in ags for r in rs]
    rules = mine or [r for ags, rs in groups if "*" in ags for r in rs]
    _ROBOTS[origin] = rules
    return rules


def robots_allowed(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    rules = _robots_rules(f"{p.scheme}://{p.netloc}")
    if rules is None:
        return False
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    best: tuple[int, bool] | None = None
    for allow, pat in rules:
        rx = re.escape(pat).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, path):
            if best is None or len(pat) > best[0] or (len(pat) == best[0] and allow):
                best = (len(pat), allow)
    return best is None or best[1]


def http_json(url: str, **kw):
    return json.loads(http_get(url, headers={"Accept": "application/json", **kw.pop("headers", {})}, **kw))


@dataclass
class Buzz:
    """その記事が話題になった場所と数値。"""
    platform: str          # 表示名（例: "Hacker News"）
    url: str | None        # 議論ページ・ランキングページの URL
    metric: str            # 表示用の数値（例: "812 points / 340 comments"）


@dataclass
class Candidate:
    source: str            # 取得元キー（config の [sources] と同じ）
    title: str
    url: str               # 元記事 URL
    score: float           # 取得元内の生スコア（points, bookmarks, stars today 等）
    buzz: list[Buzz] = field(default_factory=list)
    published: str | None = None
    lang: str = "en"
    description: str = ""  # 取得元が持つ短い説明（リポジトリ説明、論文要旨の冒頭など）
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        d = dict(d)
        d["buzz"] = [Buzz(**b) for b in d.get("buzz", [])]
        return Candidate(**d)
