"""取得元ごとの候補取得。どれも認証不要・1日1回。1つの相手（ホスト）への要求は1〜2回。

各取得元の規約上の扱い（2026-09-28 に確認）:
- Hacker News: Algolia の公開 API を使う。HN 本体の HTML は利用規約でスクレイピング禁止なので取得しない。
- GitHub: 公式 Search API を使う。Trending ページの HTML 取得は GitHub AUP の許可用途（研究・アーカイブ）に入らないため使わない。
- GitHub の急上昇: Trending ページの代わりに、GitHub の公開イベントを集めた公開データ（GH Archive 由来。ClickHouse の
  公開プレイグラウンド）で直近24時間のスター数を数える。全件ではなく標本なので、数は「標本内の件数」として表示する。
- 企業のテックブログ / 公式ブログ: 各サイトが配信している RSS・Atom だけを読む（robots.txt で拒否されていれば読まない）。
  はてなブックマーク数は公式の件数取得 API で取る。
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
import logging
import re
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .common import Buzz, Candidate, http_get, http_get_bytes, http_json, robots_allowed, to_local_date

log = logging.getLogger("daily_trend")


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


# ---------- GitHub (Search API: 最近作られたリポジトリを、1日あたりのスター数順) ----------
_GH_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
GH_NEW_DAYS = 30      # 「最近作られた」とみなす日数
GH_MIN_STARS = 300    # 30日以内の検索で拾う下限（3日以内の検索には下限を付けない）


def _gh_search(q: str, per_page: int) -> list[dict]:
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": per_page})
    return http_json(url, headers=_GH_HEADERS).get("items", [])


def _gh_title(x: dict) -> str:
    return x["full_name"] + (f" — {x['description']}" if x.get("description") else "")


def _gh_description(x: dict) -> str:
    return (x.get("description") or "") + f" [language: {x.get('language')}, topics: {', '.join(x.get('topics') or [])}]"


def github_new_repos(items: list[dict], now: datetime, web: str) -> list[Candidate]:
    """検索結果を1日あたりのスター数で並べる。作成から1日未満は1日として割る。"""
    out, seen = [], set()
    for x in items:
        if x["full_name"] in seen:
            continue
        seen.add(x["full_name"])
        stars = x.get("stargazers_count") or 0
        created = datetime.fromisoformat(x["created_at"].replace("Z", "+00:00"))
        per_day = stars / max((now - created).total_seconds() / 86400, 1.0)
        out.append(Candidate(
            source="github", title=_gh_title(x), url=x["html_url"], score=per_day,
            buzz=[Buzz(f"GitHub（{GH_NEW_DAYS}日以内に作成されたリポジトリ。1日あたりのスター数順）", web,
                       f"★{stars}（{to_local_date(x.get('created_at'))} 作成、1日あたり約 {per_day:.0f}）")],
            published=to_local_date(x.get("created_at")), lang="en", description=_gh_description(x),
        ))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:30]


def github() -> list[Candidate]:
    today = datetime.now(timezone.utc).date()  # created: は UTC 基準
    q_month = f"created:>={(today - timedelta(days=GH_NEW_DAYS)).isoformat()} stars:>={GH_MIN_STARS}"
    q_fresh = f"created:>={(today - timedelta(days=3)).isoformat()}"  # 出来たばかりで、まだスターが少ないもの
    items = _gh_search(q_month, 50) + _gh_search(q_fresh, 20)
    web = "https://github.com/search?" + urllib.parse.urlencode({"q": q_month, "type": "repositories", "s": "stars", "o": "desc"})
    return github_new_repos(items, datetime.now(timezone.utc), web)


# ---------- GitHub の急上昇（作成時期を問わず、直近24時間にスターが多く付いたリポジトリ）----------
_GH_EVENTS_SQL = ("SELECT repo_name, uniq(actor_login) AS n FROM github_events "
                  "WHERE event_type = 'WatchEvent' AND created_at > now() - INTERVAL 1 DAY "
                  "GROUP BY repo_name ORDER BY n DESC LIMIT 25 FORMAT JSON")
GH_TREND_MIN_SAMPLE = 15   # 1位がこれ未満なら、標本が少なすぎて順位が当てにならないので使わない


def github_trending() -> list[Candidate]:
    rows = http_json("https://play.clickhouse.com/?" + urllib.parse.urlencode({"user": "play", "query": _GH_EVENTS_SQL}),
                     timeout=30).get("data", [])
    rows = [(r["repo_name"], int(r["n"])) for r in rows if re.fullmatch(r"[\w.-]+/[\w.-]+", r.get("repo_name") or "")]
    if not rows or rows[0][1] < GH_TREND_MIN_SAMPLE:
        raise RuntimeError(f"star events sample too small (top={rows[0][1] if rows else 0})")
    rows = rows[:20]
    # 説明・累計スター数は GitHub の公式 API で取る（消えたリポジトリは結果に出ないので、そのまま落とす）
    info = {x["full_name"].lower(): x for x in _gh_search(" ".join(f"repo:{name}" for name, _ in rows), len(rows))}
    out = []
    for rank_, (name, n) in enumerate(rows, 1):
        x = info.get(name.lower())
        if x is None:
            continue
        out.append(Candidate(
            source="github_trending", title=_gh_title(x), url=x["html_url"], score=n,
            buzz=[Buzz("GitHub（直近24時間にスターが多く付いたリポジトリ。公開イベントの標本集計）", x["html_url"] + "/stargazers",
                       f"{rank_} 位（標本内 {n} 件）/ 累計 ★{x.get('stargazers_count')}")],
            published=to_local_date(x.get("created_at")), lang="en", description=_gh_description(x),
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


# ---------- 企業のテックブログ / 公式ブログ（config.toml の [feeds.*] に書いた RSS・Atom）----------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _feed_date(s: str | None) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)  # RSS 2.0（RFC 822）
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom / RSS 1.0（ISO 8601）
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _plain(s: str | None, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))).strip()[:limit]


def parse_feed(raw: bytes) -> list[dict]:
    """RSS 2.0 / RSS 1.0 / Atom の記事一覧。公開日が読めない記事は、新しいかどうか分からないので捨てる。"""
    out = []
    for el in ET.fromstring(raw).iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = link = summary = ""
        dates: dict[str, datetime] = {}
        for ch in el:
            name = _local(ch.tag)
            if name == "title":
                title = _plain("".join(ch.itertext()), 200)
            elif name == "link":
                href = ch.get("href")  # Atom は属性、RSS は中身
                if href is None:
                    link = link or (ch.text or "").strip()
                elif ch.get("rel") in (None, "alternate"):
                    link = href.strip()
            elif name in ("pubDate", "published", "date", "updated"):
                d = _feed_date(ch.text)
                if d:
                    dates[name] = d
            elif name in ("description", "summary") and not summary:
                summary = _plain("".join(ch.itertext()))
        when = next((dates[k] for k in ("pubDate", "published", "date", "updated") if k in dates), None)
        if title and link.startswith(("http://", "https://")) and when:
            out.append({"title": title, "url": link, "published": when, "summary": summary})
    return out


def hatena_counts(urls: list[str]) -> dict[str, int]:
    """はてなブックマーク数（公式の件数取得 API。1回に 50 URL まで）。取れなければ 0 件として扱う。"""
    counts: dict[str, int] = {}
    for i in range(0, len(urls), 50):
        qs = urllib.parse.urlencode([("url", u) for u in urls[i:i + 50]])
        try:
            counts.update(http_json("https://bookmark.hatenaapis.com/count/entries?" + qs, timeout=15))
        except Exception as e:
            log.warning("hatena count failed: %s", e)
    return counts


def _hatena_entry_url(link: str) -> str:
    return "https://b.hatena.ne.jp/entry/" + ("s/" + link[8:] if link.startswith("https://") else link[7:])


def feed_candidates(source: str, feeds: dict[str, str], max_age_days: int, lang: str) -> list[Candidate]:
    """feeds = {表示名: フィードの URL}。max_age_days 日以内の記事を、はてブ数と新しさで並べる。"""
    now = datetime.now(timezone.utc)

    def one(item: tuple[str, str]) -> list[dict]:
        name, url = item
        try:
            if not robots_allowed(url):
                raise RuntimeError("robots.txt disallows the feed")
            raw, _ = http_get_bytes(url, timeout=15, retries=1, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
            entries = parse_feed(raw)
        except Exception as e:  # 1つのフィードが落ちても他は続ける
            log.warning("feed %s (%s) failed: %s: %s", name, url, type(e).__name__, e)
            return []
        fresh = [dict(e, feed=name, feed_url=url) for e in entries
                 if timedelta(0) <= now - e["published"] <= timedelta(days=max_age_days)]
        return fresh[:5]  # 1つのブログが候補を占めないようにする

    with ThreadPoolExecutor(max_workers=8) as ex:
        entries = [e for chunk in ex.map(one, feeds.items()) for e in chunk]
    counts = hatena_counts([e["url"] for e in entries])
    out = []
    for e in entries:
        n = int(counts.get(e["url"]) or 0)
        age_days = (now - e["published"]).total_seconds() / 86400
        day = e["published"].astimezone().strftime("%Y-%m-%d")
        buzz = [Buzz(f"{e['feed']}（公式フィード）", e["feed_url"], f"{day} 公開")]
        if n:
            buzz.append(Buzz("はてなブックマーク", _hatena_entry_url(e["url"]), f"{n} users"))
        out.append(Candidate(
            source=source, title=e["title"], url=e["url"], score=(n + 1) / (1 + age_days),
            buzz=buzz, published=day, lang=lang, description=f"{e['feed']}: {e['summary']}",
        ))
    return out


FETCHERS = {
    "hackernews": hackernews,
    "github": github,
    "github_trending": github_trending,
    "hf_papers": hf_papers,
    "hf_models": hf_models,
    "hatena": hatena,
    "zenn": zenn,
    "qiita": qiita,
    "techmeme": techmeme,
}

# config.toml の [feeds.<キー>] を読む取得元。値は記事の言語（要約前の目安）
FEED_SOURCES = {"techblogs": "ja", "official": "en"}


def feed_urls(feeds: dict, key: str) -> dict[str, str]:
    """config.toml の [feeds.<key>] から {表示名: URL}。書き方が違う行は無視する。"""
    section = feeds.get(key)
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if isinstance(v, str) and v.startswith(("http://", "https://"))}


def fetch_all(enabled: dict, feeds: dict | None = None) -> tuple[list[Candidate], dict[str, str]]:
    feeds = feeds or {}
    max_age = feeds.get("max_age_days", 7)
    max_age = max_age if isinstance(max_age, int) and max_age > 0 else 7
    fetchers = dict(FETCHERS)
    for key, lang in FEED_SOURCES.items():
        urls = feed_urls(feeds, key)
        if urls:  # フィードが1つも書かれていない取得元は、無いものとして扱う
            fetchers[key] = lambda key=key, urls=urls, lang=lang: feed_candidates(key, urls, max_age, lang)
    keys = [k for k in fetchers if enabled.get(k, True)]
    cands, errors = [], {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(fetchers[k]) for k in keys}
        for k, f in futs.items():
            try:
                items = f.result(timeout=90)
                if not items:
                    errors[k] = "0 items"
                cands.extend(items)
            except Exception as e:  # 1つの取得元が落ちても他は続ける
                errors[k] = f"{type(e).__name__}: {e}"
    return cands, errors
