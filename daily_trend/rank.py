"""候補の統合・順位付け・既出除外。LLM を使わない決定的な処理。"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import Candidate
from .extract import github_parts, is_repo_top

_TRACKING = re.compile(r"^(utm_|ref$|ref_src$|source$|fbclid$|gclid$|mc_cid$|mc_eid$)")


# 本文を取得しない（規約・robots.txt で自動取得が拒否されている）ホスト。候補の段階で除く。
DENY_HOSTS = ("reddit.com", "x.com", "twitter.com", "lobste.rs", "facebook.com", "instagram.com", "threads.net")


def denied(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in DENY_HOSTS)


def canonical_url(url: str) -> str:
    """同じ記事を別 URL で数えないための正規化。別の記事を同一視しないことを優先する。"""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = re.sub(r"/+$", "", p.path) or "/"
    # arXiv は abs/pdf/html と版番号を同一視する
    if host in ("arxiv.org", "export.arxiv.org"):
        m = re.search(r"(\d{4}\.\d{4,5})", path)
        if m:
            return f"arxiv:{m.group(1)}"
    if host == "huggingface.co":
        m = re.match(r"/papers/(\d{4}\.\d{4,5})", path)
        if m:
            return f"arxiv:{m.group(1)}"
    if host == "github.com":
        parts = github_parts(url)
        if parts and is_repo_top(parts):  # issue・PR・リリース等は別の記事として扱う
            return f"github:{parts[0].lower()}/{parts[1].lower()}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)))
    frag = p.fragment if p.fragment.startswith(("/", "!")) else ""  # #/post/1 のようなハッシュルーティングは残す
    return urlunsplit(("", host, path, query, frag))


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"^(show|ask|tell) hn:\s*", "", t)
    return re.sub(r"[\W_]+", " ", t).strip()


def merge(cands: list[Candidate]) -> list[Candidate]:
    """同じ記事が複数ソースに出ていたら1件にまとめ、話題元(buzz)を合算する。"""
    by_key: dict[str, Candidate] = {}
    for c in cands:
        key = canonical_url(c.url)
        if key not in by_key:
            c.extra.setdefault("sources", [c.source])
            c.extra.setdefault("norm", {})
            by_key[key] = c
            continue
        base = by_key[key]
        if c.source not in base.extra["sources"]:
            base.extra["sources"].append(c.source)
        known = {(b.platform, b.url) for b in base.buzz}
        base.buzz.extend(b for b in c.buzz if (b.platform, b.url) not in known)
        if not base.description and c.description:
            base.description = c.description
        if not base.published and c.published:
            base.published = c.published
        base.extra.setdefault("alt_titles", []).append(c.title)
        base.extra["norm"].update(c.extra.get("norm", {}))
    return list(by_key.values())


def score_all(cands: list[Candidate]) -> None:
    """ソースごとに順位を 0〜1 に正規化し、複数ソースに出たものを加点する。"""
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        groups[c.source].append(c)
    for src, items in groups.items():
        items.sort(key=lambda c: c.score, reverse=True)
        n = len(items)
        for i, c in enumerate(items):
            c.extra.setdefault("norm", {})[src] = 1.0 - i / max(n, 1)


def combined(c: Candidate) -> float:
    norms = sorted(c.extra.get("norm", {}).values(), reverse=True)
    if not norms:
        return 0.0
    return norms[0] + 0.35 * sum(norms[1:]) + 0.15 * (len(norms) - 1)


def exclude_history(cands: list[Candidate], history: list[dict]) -> tuple[list[Candidate], list[str]]:
    """過去に選んだ記事（URL 一致・題名がほぼ同じ）を除く。"""
    past_keys = {canonical_url(h["url"]) for h in history if h.get("url")}
    past_titles = [norm_title(h["title"]) for h in history if h.get("title")]
    kept, dropped = [], []
    for c in cands:
        if canonical_url(c.url) in past_keys:
            dropped.append(c.title)
            continue
        nt = norm_title(c.title)
        if nt and any(difflib.SequenceMatcher(None, nt, pt).ratio() > 0.85 for pt in past_titles):
            dropped.append(c.title)
            continue
        kept.append(c)
    return kept, dropped


def shortlist(cands: list[Candidate], history: list[dict], limit: int, per_source_cap: int = 10) -> tuple[list[Candidate], list[str]]:
    cands = [c for c in cands if not denied(c.url)]
    score_all(cands)  # 統合前に各ソース内の順位を出す（統合後は複数ソースの値を持つ）
    merged = merge(cands)
    merged, dropped = exclude_history(merged, history)
    merged.sort(key=combined, reverse=True)
    out, per_src = [], defaultdict(int)
    for c in merged:
        primary = c.extra["sources"][0]
        if per_src[primary] >= per_source_cap and len(c.extra["sources"]) == 1:
            continue
        per_src[primary] += 1
        out.append(c)
        if len(out) >= limit:
            break
    return out, dropped
