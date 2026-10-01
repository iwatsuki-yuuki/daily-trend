"""取得元ごとの候補取得。どれも認証不要・1日1回・各1〜2リクエスト。

各取得元の規約上の扱い（2026-09-28 に確認）:
- Hacker News: Algolia の公開 API を使う。HN 本体の HTML は利用規約でスクレイピング禁止なので取得しない。
- GitHub: 公式 Search API を使う。Trending ページの HTML 取得は GitHub AUP の許可用途（研究・アーカイブ）に入らないため使わない。
- Hugging Face: 公開 API（/api/daily_papers, /api/models）。
- はてなブックマーク / Zenn / Qiita: 公式 RSS・フィード（Zenn は広く使われている非公式 API）。本文は保存・転載しない。
- Lobsters: 使わない。robots.txt が一般ボットに全パス（RSS を含む）を拒否し、ai-input=no を宣言している。
- Techmeme: robots.txt で許可されたトップページ（/）。各話題に付いた X 投稿・他媒体・掲示板のリンク数を
  「X での反応」の代わりに使う（X の API は使わず、X の投稿本文も取得しない）。
- Reddit: 使わない。非認証 JSON は 403、User Agreement が事前同意のないスクレイピングを禁止している。

日付: published には記事そのものの公開日だけを入れる。HN の投稿日・はてブの登録日は extra["listed_at"] に入れ、
「公開日」としては表示しない。UTC の時刻はローカル日付に直す。
"""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .common import Buzz, Candidate, http_get, http_json, to_local_date


# ---------- Hacker News (Algolia) ----------
def hackernews() -> list[Candidate]:
    since = int(time.time()) - 24 * 3600
    qs = urllib.parse.urlencode({"tags": "story", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 40})
    hits = http_json("https://hn.algolia.com/api/v1/search?" + qs).get("hits", [])
    out = []
    for h in hits:
        disc = f"https://news.ycombinator.com/item?id={h['objectID']}"
        title = h.get("title") or ""
        pts, com = h.get("points") or 0, h.get("num_comments") or 0
        score = pts * (0.6 if re.search(r"\((19|20)\d\d\)\s*$", title) else 1.0)  # "(2019)" 付きは古い記事の再投稿
        out.append(Candidate(
            source="hackernews", title=title, url=h.get("url") or disc, score=score,
            buzz=[Buzz("Hacker News", disc, f"{pts} points / {com} comments")],
            lang="en",
            description=re.sub(r"<[^>]+>", " ", html.unescape(h.get("story_text") or ""))[:300],
            extra={"hn_id": h["objectID"], "listed_at": to_local_date(h.get("created_at")), "listed_on": "Hacker News"},
        ))
    return out


# ---------- GitHub (Search API: 直近に作られたリポジトリをスター順) ----------
def github() -> list[Candidate]:
    since = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()  # created: は UTC 基準
    q = f"created:>={since}"
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": 20})
    data = http_json(url, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    web = "https://github.com/search?" + urllib.parse.urlencode({"q": q, "type": "repositories", "s": "stars", "o": "desc"})
    out = []
    for x in data.get("items", []):
        out.append(Candidate(
            source="github", title=x["full_name"] + (f" — {x['description']}" if x.get("description") else ""),
            url=x["html_url"], score=x.get("stargazers_count") or 0,
            buzz=[Buzz("GitHub（3日以内に作成されたリポジトリのスター数順）", web,
                       f"★{x.get('stargazers_count')}（{to_local_date(x.get('created_at'))} 作成）")],
            published=to_local_date(x.get("created_at")), lang="en",
            description=(x.get("description") or "") + f" [language: {x.get('language')}, topics: {', '.join(x.get('topics') or [])}]",
        ))
    return out


# ---------- Hugging Face ----------
def hf_papers() -> list[Candidate]:
    # date は UTC の今日以下でないと 400。朝は upvote が伸びていないので UTC の前日から遡る。土日は空配列。
    d = datetime.now(timezone.utc).date() - timedelta(days=1)
    data: list = []
    for _ in range(6):
        try:
            data = http_json("https://huggingface.co/api/daily_papers?" + urllib.parse.urlencode({"date": d.isoformat(), "limit": 100}))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            data = []
        if data:
            break
        d -= timedelta(days=1)
    out = []
    for e in data:
        p = e.get("paper") or {}
        pid = p.get("id") or ""
        if not re.fullmatch(r"\d{4}\.\d{4,5}", pid):
            continue
        up = int(p.get("upvotes") or 0)
        out.append(Candidate(
            source="hf_papers", title=(p.get("title") or e.get("title") or "").strip(),
            url=f"https://arxiv.org/abs/{pid}", score=up,
            buzz=[Buzz("Hugging Face Daily Papers", f"https://huggingface.co/papers/{pid}",
                       f"{up} upvotes（{d.isoformat()} 掲載分）")],
            published=to_local_date(p.get("publishedAt")), lang="en",
            description=re.sub(r"\s+", " ", p.get("summary") or "")[:300],
        ))
    return out


_HF_NOISE = re.compile(r"(gguf|uncensored|abliterated|-awq|-gptq|-exl2|-mlx|-fp8|-int4|-4bit|-8bit)", re.I)


def hf_models() -> list[Candidate]:
    data = http_json("https://huggingface.co/api/models?" + urllib.parse.urlencode({"sort": "trendingScore", "limit": 30}))
    out = []
    for m in data:
        mid = m.get("id") or m.get("modelId") or ""
        tags = m.get("tags") or []
        if _HF_NOISE.search(mid) or "gguf" in tags or any(t.startswith("base_model:") for t in tags):
            continue  # 量子化版・再配布・派生モデルを除く
        out.append(Candidate(
            source="hf_models", title=mid, url=f"https://huggingface.co/{mid}", score=float(m.get("trendingScore") or 0),
            buzz=[Buzz("Hugging Face Trending", "https://huggingface.co/models?sort=trending",
                       f"trending score {m.get('trendingScore')} / {m.get('likes')} likes")],
            published=to_local_date(m.get("createdAt")), lang="en",
            description=f"pipeline: {m.get('pipeline_tag')}",
        ))
    return out[:12]


# ---------- はてなブックマーク（テクノロジー 人気エントリー）----------
_HNS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "hatena": "http://www.hatena.ne.jp/info/xmlns#",
}


def hatena() -> list[Candidate]:
    root = ET.fromstring(http_get("https://b.hatena.ne.jp/hotentry/it.rss").encode())
    out = []
    for it in root.findall("rss:item", _HNS):
        link = (it.findtext("rss:link", namespaces=_HNS) or it.get(f"{{{_HNS['rdf']}}}about") or "").strip()
        if not link or "anond.hatelabo.jp" in link:
            continue
        disc = (it.findtext("hatena:bookmarkCommentListPageUrl", namespaces=_HNS) or "").strip()
        if not disc:
            disc = "https://b.hatena.ne.jp/entry/" + ("s/" + link[8:] if link.startswith("https://") else link[7:])
        cnt = (it.findtext("hatena:bookmarkcount", namespaces=_HNS) or "0").strip()
        n = int(cnt) if cnt.isdigit() else 0
        tags = [s.text for s in it.findall("dc:subject", _HNS) if s.text][1:]  # 先頭はカテゴリ名
        out.append(Candidate(
            source="hatena", title=(it.findtext("rss:title", namespaces=_HNS) or "").strip(), url=link, score=n,
            buzz=[Buzz("はてなブックマーク", disc, f"{n} users")],
            lang="ja",
            description=f"tags: {', '.join(tags)}",
            extra={"listed_at": to_local_date(it.findtext("dc:date", namespaces=_HNS)), "listed_on": "はてなブックマーク"},
        ))
    return out


# ---------- Zenn（デイリートレンド）----------
def zenn() -> list[Candidate]:
    data = http_json("https://zenn.dev/api/articles?order=daily")
    out = []
    for rank_, a in enumerate(x for x in data.get("articles", []) if x.get("article_type") == "tech"):
        url = "https://zenn.dev" + a["path"]
        liked = a.get("liked_count", 0)
        out.append(Candidate(
            source="zenn", title=a["title"], url=url, score=liked,
            buzz=[Buzz("Zenn トレンド", "https://zenn.dev/", f"{liked} いいね（デイリートレンド {rank_ + 1} 位）")],
            published=to_local_date(a.get("published_at")), lang="ja",
        ))
    return out


# ---------- Qiita（人気記事フィード）----------
def qiita() -> list[Candidate]:
    A = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(http_get("https://qiita.com/popular-items/feed").encode())
    out = []
    entries = root.findall(A + "entry")
    for i, e in enumerate(entries):
        ln = e.find(A + "link[@rel='alternate']")
        if ln is None:
            continue
        p = urllib.parse.urlsplit(ln.get("href"))
        url = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        out.append(Candidate(
            source="qiita", title=e.findtext(A + "title") or "", url=url, score=len(entries) - i,  # 並び順 = トレンド順
            buzz=[Buzz("Qiita トレンド", "https://qiita.com/", f"トレンド {i + 1} 位")],
            published=to_local_date(e.findtext(A + "published")), lang="ja",
        ))
    return out


# ---------- Techmeme（トップページの Top News。X での反応数を付ける）----------
def _tm_section(chunk: str, label: str) -> list[str]:
    m = re.search(r'<SPAN CLASS="drhed">%s</SPAN>(.*?)</span>' % re.escape(label), chunk, re.S)
    return [html.unescape(u) for u in re.findall(r'HREF="([^"]+)"', m.group(1))] if m else []


def techmeme() -> list[Candidate]:
    page = http_get("https://www.techmeme.com/")
    out = []
    chunks = page.split('<DIV CLASS="clus">')[1:]
    for rank_, c in enumerate(chunks, 1):
        m = re.search(r'<STRONG CLASS="L\d+"><A CLASS="ourh" HREF="([^"]+)">(.*?)</A>', c, re.S)
        if not m:
            continue
        p = re.search(r'pml="(\d{6})(p\d+)"', c)
        perma = f"https://www.techmeme.com/{p.group(1)}/{p.group(2)}#a{p.group(1)}{p.group(2)}" if p else "https://www.techmeme.com/"
        xs = [u for u in _tm_section(c, "X:") if re.search(r"(x|twitter)\.com/[^/]+/status/\d+", u)]
        more, forums = _tm_section(c, "More:"), _tm_section(c, "Forums:")
        cite = re.search(r"<CITE>(.*?)</CITE>", c, re.S)
        buzz = [Buzz("Techmeme", perma, f"Top News {rank_} 位 / 他媒体 {len(more)} 件")]
        if xs:
            buzz.append(Buzz("X（Techmeme が収集した反応投稿）", perma, f"{len(xs)} 件の投稿"))
        for f in forums:  # HN / Reddit のスレッドもそのまま話題元として載せる
            if "news.ycombinator.com/item" in f:
                buzz.append(Buzz("Hacker News", f, "Techmeme 経由"))
        out.append(Candidate(
            source="techmeme", title=html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
            url=html.unescape(m.group(1)), score=100 - rank_ + 2 * len(xs) + len(more),
            buzz=buzz, lang="en",
            description=f"outlet: {html.unescape(re.sub(r'<[^>]+>', '', cite.group(1))).strip() if cite else ''}",
            extra={"x_posts": xs[:5]},
        ))
    if not out:
        raise RuntimeError("Techmeme: 0 clusters (HTML structure changed?)")
    return out


FETCHERS = {
    "hackernews": hackernews,
    "github": github,
    "hf_papers": hf_papers,
    "hf_models": hf_models,
    "hatena": hatena,
    "zenn": zenn,
    "qiita": qiita,
    "techmeme": techmeme,
}


def fetch_all(enabled: dict) -> tuple[list[Candidate], dict[str, str]]:
    keys = [k for k in FETCHERS if enabled.get(k, True)]
    cands, errors = [], {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(FETCHERS[k]) for k in keys}
        for k, f in futs.items():
            try:
                items = f.result(timeout=90)
                if not items:
                    errors[k] = "0 items"
                cands.extend(items)
            except Exception as e:  # 1つの取得元が落ちても他は続ける
                errors[k] = f"{type(e).__name__}: {e}"
    return cands, errors
