"""候補の統合・順位付け・既出除外・種類ごとの重み付け。LLM を使わない決定的な処理。"""
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


# ---------- 種類（何を重視して選ぶかの単位）----------
KINDS = {"news": "ニュース・発表", "blogs": "テックブログ", "oss": "OSS・公開モデル", "papers": "論文"}

# 取得元ごとの既定の種類。URL で決まる場合（arXiv・GitHub のリポジトリ・ブログのホスト）はそちらを優先する
SOURCE_KIND = {"hackernews": "news", "techmeme": "news", "hatena": "news", "official": "news",
               "zenn": "blogs", "qiita": "blogs", "techblogs": "blogs",
               "github": "oss", "github_trending": "oss", "hf_models": "oss", "hf_papers": "papers"}

BLOG_HOSTS = ("zenn.dev", "qiita.com", "note.com", "hatenablog.com", "hatenablog.jp", "hateblo.jp", "hatenadiary.com",
              "medium.com", "dev.to", "substack.com")

FOCUS_PRESETS = {name: {k: float(name in ("balanced", k)) for k in KINDS} for name in ("balanced", *KINDS)}

_FOCUS_HINTS = {
    "news": "今回は「新しい発表・リリース」から選ぶ。新しいモデル・製品・バージョンの公開など、何が出たのかが具体的な候補を優先する。",
    "blogs": "今回は「技術ブログ」から選ぶ。実装・設計・運用の知見が具体的に書かれていて、読者が学べる記事を優先する。"
             "発表やリリースであるかどうかは問わない。採用・イベント告知・参加レポートだけの記事は優先度を下げる。",
    "oss": "今回は「OSS・公開モデル」から選ぶ。何ができるものかが説明から具体的に分かり、スターや話題の伸びが大きいものを優先する。",
    "papers": "今回は「論文」から選ぶ。手法と結果が具体的なものを優先する。",
}


def focus_weights(pick_cfg: dict, override: str | None = None) -> tuple[str, dict[str, float]]:
    """(focus の名前, 種類ごとの重み)。focus = "custom" のときは [pick.weights] を使う（書いていない種類は 1）。"""
    name = override or str(pick_cfg.get("focus", "balanced"))
    if name == "custom":
        raw = pick_cfg.get("weights") or {}
        weights = {k: max(float(raw.get(k, 1.0)), 0.0) for k in KINDS}
        if not any(weights.values()):
            raise ValueError("[pick.weights] がすべて 0 です")
        return name, weights
    if name not in FOCUS_PRESETS:
        raise ValueError(f"focus = {name!r} は使えません（{' / '.join([*FOCUS_PRESETS, 'custom'])}）")
    return name, dict(FOCUS_PRESETS[name])


def focus_hint(name: str, weights: dict[str, float]) -> str | None:
    """選定の指示に足す1文。balanced（すべて同じ重み）のときは足さない。"""
    if name in _FOCUS_HINTS:
        return _FOCUS_HINTS[name]
    if len(set(weights.values())) <= 1:
        return None
    order = sorted((k for k in KINDS if weights[k] > 0), key=lambda k: -weights[k])
    return "今回は種類に重みを付けて選ぶ（数字が大きい種類を優先する）: " + "、".join(f"{KINDS[k]} {weights[k]:g}" for k in order)


def kind_of(c: Candidate, blog_hosts: tuple[str, ...] = ()) -> str:
    key = canonical_url(c.url)
    if key.startswith("arxiv:"):
        return "papers"
    if key.startswith("github:"):
        return "oss"
    host = (urlsplit(c.url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in (*BLOG_HOSTS, *blog_hosts)):
        return "blogs"
    return SOURCE_KIND.get((c.extra.get("sources") or [c.source])[0], "news")


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


# 順位を点数にするときの深さ。どの取得元でも「1位 = 1.0、31位以下 = 0」にする。
# 件数で割ると、件数の多い取得元（フィードを足した techblogs など）ほど候補の枠を多く取ってしまう。
NORM_DEPTH = 30

# 同じ場所を別の切り口で見ているだけの取得元は、1つとして数える（「複数の場で話題」の加点を二重にしない）
SOURCE_FAMILY = {"github_trending": "github"}
# 記事が載っていること自体は話題の根拠にならない取得元。他の取得元にも出ていれば、そちらの順位だけを使う
LISTING_ONLY = {"techblogs", "official"}


def score_all(cands: list[Candidate]) -> None:
    """ソースごとの順位を 0〜1 の点数にする。"""
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        groups[c.source].append(c)
    for src, items in groups.items():
        items.sort(key=lambda c: c.score, reverse=True)
        for i, c in enumerate(items):
            c.extra.setdefault("norm", {})[src] = max(1.0 - i / NORM_DEPTH, 0.0)


def _signals(c: Candidate) -> list[float]:
    """話題の根拠として数える点数（SOURCE_FAMILY ごとに1つ。大きい順）。"""
    norm = c.extra.get("norm", {})
    if any(s not in LISTING_ONLY for s in norm):
        norm = {s: v for s, v in norm.items() if s not in LISTING_ONLY}
    best: dict[str, float] = {}
    for s, v in norm.items():
        fam = SOURCE_FAMILY.get(s, s)
        best[fam] = max(best.get(fam, 0.0), v)
    return sorted(best.values(), reverse=True)


def combined(c: Candidate) -> float:
    """複数の場に出たものを加点する。"""
    norms = _signals(c)
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


def shortlist(cands: list[Candidate], history: list[dict], limit: int, per_source_cap: int = 10,
              weights: dict[str, float] | None = None, blog_hosts: tuple[str, ...] = ()) -> tuple[list[Candidate], list[str]]:
    """点数の高い順に limit 件。同じ点数なら取得元の並び順なので、各取得元の上位がほぼ同じ件数ずつ入る。
    weights = 種類ごとの重み（rank.KINDS のキー）。0 の種類は候補から外し、それ以外は点数に掛ける。"""
    weights = weights or {}
    cands = [c for c in cands if not denied(c.url)]
    score_all(cands)  # 統合前に各ソース内の順位を出す（統合後は複数ソースの値を持つ）
    merged = merge(cands)
    merged, dropped = exclude_history(merged, history)
    for c in merged:
        c.extra["kind"] = kind_of(c, blog_hosts)
    merged = [c for c in merged if weights.get(c.extra["kind"], 1.0) > 0]
    merged.sort(key=lambda c: combined(c) * weights.get(c.extra["kind"], 1.0), reverse=True)
    # 取得元が少ないとき（種類を絞ったとき）は、1つの取得元から多めに取る
    n_sources = len({c.extra["sources"][0] for c in merged})
    cap = max(per_source_cap, -(-limit // max(n_sources, 1)))
    out, per_src = [], defaultdict(int)
    for c in merged:
        primary = c.extra["sources"][0]
        if per_src[primary] >= cap and len(_signals(c)) == 1:
            continue
        per_src[primary] += 1
        out.append(c)
        if len(out) >= limit:
            break
    return out, dropped
