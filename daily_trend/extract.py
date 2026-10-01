"""選んだ候補の本文を取得する。要約はこの本文だけを根拠にする。

取得の方針:
- GitHub・Hugging Face・arXiv・HN の自己投稿は、それぞれの公式 API で取る。
- それ以外の記事ページは、robots.txt で許可されている場合だけ取る。
- 公開日は記事のメタデータ（meta タグ・JSON-LD）にあるものだけを使う。HN の投稿日やはてブの登録日では埋めない。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import trafilatura

from .common import Candidate, http_get, http_get_bytes, http_json, robots_allowed, to_local_date

MAX_CHARS = 60_000   # 1分要約には十分。超えたら truncated=True として記録する

# リポジトリのトップとして扱わない GitHub の予約パス
GITHUB_RESERVED = {"orgs", "features", "marketplace", "sponsors", "topics", "collections", "apps", "settings",
                   "security", "advisories", "about", "pricing", "enterprise", "login", "search", "trending",
                   "explore", "notifications", "users", "site", "customer-stories", "readme", "events"}


@dataclass
class Article:
    text: str
    title: str
    site: str
    published_at: str | None
    lang: str
    truncated: bool
    kind: str            # "article" | "readme" | "abstract" | "hn_text" | "github_issue" | "github_release"


class ExtractError(Exception):
    pass


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _cap(text: str) -> tuple[str, bool]:
    text = text.strip()
    return (text[:MAX_CHARS], True) if len(text) > MAX_CHARS else (text, False)


def github_parts(url: str) -> list[str] | None:
    """github.com の URL なら、予約パスを除いたパス要素を返す。"""
    p = urlsplit(url)
    if (p.hostname or "").removeprefix("www.") != "github.com":
        return None
    parts = [s for s in p.path.split("/") if s]
    if len(parts) < 2 or parts[0].lower() in GITHUB_RESERVED:
        return None
    return parts


def is_repo_top(parts: list[str]) -> bool:
    """リポジトリのトップ（/owner/repo と /owner/repo/tree/<branch>）か。"""
    return len(parts) == 2 or (len(parts) == 4 and parts[2] == "tree")


def _gh_api(path: str, raw: bool = False):
    hdrs = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}
    url = "https://api.github.com" + path
    return http_get(url, headers=hdrs) if raw else http_json(url, headers=hdrs)


def _github_repo(owner: str, repo: str) -> Article:
    meta = _gh_api(f"/repos/{owner}/{repo}")
    readme = _gh_api(f"/repos/{owner}/{repo}/readme", raw=True)
    head = (f"Repository: {owner}/{repo}\nDescription: {meta.get('description') or ''}\n"
            f"Stars: {meta.get('stargazers_count')}  Language: {meta.get('language')}  "
            f"Created: {meta.get('created_at')}  License: {(meta.get('license') or {}).get('spdx_id')}\n\n")
    text, trunc = _cap(head + readme)
    return Article(text, f"{owner}/{repo}", "github.com", to_local_date(meta.get("created_at")), "en", trunc, "readme")


def _github_sub(parts: list[str]) -> Article:
    """issue / PR / リリース / ファイルのページ。HTML ではなく公式 API で本文を取る（GitHub AUP に従う）。"""
    owner, repo, kind = parts[0], parts[1], parts[2]
    if kind in ("issues", "pull") and len(parts) >= 4 and parts[3].isdigit():
        d = _gh_api(f"/repos/{owner}/{repo}/issues/{parts[3]}")  # PR も issues API で本文が取れる
        label = "Pull request" if kind == "pull" else "Issue"
        text, trunc = _cap(f"{label} #{parts[3]} in {owner}/{repo}: {d.get('title')}\n\n{d.get('body') or ''}")
        return Article(text, f"{owner}/{repo}#{parts[3]}: {d.get('title')}", "github.com",
                       to_local_date(d.get("created_at")), "en", trunc, "github_issue")
    if kind == "releases" and len(parts) >= 5 and parts[3] == "tag":
        tag = "/".join(parts[4:])
        d = _gh_api(f"/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}")
        text, trunc = _cap(f"Release {d.get('name') or tag} of {owner}/{repo}\n\n{d.get('body') or ''}")
        return Article(text, f"{owner}/{repo} {d.get('name') or tag}", "github.com",
                       to_local_date(d.get("published_at")), "en", trunc, "github_release")
    if kind == "blob" and len(parts) >= 5:
        ref, path = parts[3], "/".join(parts[4:])
        body = _gh_api(f"/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(ref, safe='')}", raw=True)
        text, trunc = _cap(f"File {path} in {owner}/{repo}\n\n{body}")
        return Article(text, f"{owner}/{repo}: {path}", "github.com", None, "en", trunc, "readme")
    raise ExtractError(f"unsupported GitHub page type: /{'/'.join(parts)}")


def _arxiv_id(c: Candidate) -> str | None:
    m = re.search(r"(\d{4}\.\d{4,5})", c.url)
    return m.group(1) if m and ("arxiv.org" in c.url or "huggingface.co/papers" in c.url) else None


def _paper(c: Candidate, pid: str) -> Article:
    try:
        d = http_json(f"https://huggingface.co/api/papers/{pid}")
        title, abstract = d.get("title", c.title), d.get("summary", "")
        published = to_local_date(d.get("publishedAt"))
    except Exception:
        # HF に無い論文は arXiv の公式 API（Atom）から要旨を取る
        xml = http_get(f"https://export.arxiv.org/api/query?id_list={pid}")
        title = re.search(r"<entry>.*?<title>(.*?)</title>", xml, re.S).group(1)
        abstract = re.search(r"<summary>(.*?)</summary>", xml, re.S).group(1)
        published = re.search(r"<published>(\d{4}-\d{2}-\d{2})", xml).group(1)
    title = re.sub(r"\s+", " ", title).strip()
    abstract = re.sub(r"\s+", " ", abstract).strip()
    if len(abstract) < 200:
        raise ExtractError(f"abstract too short for {pid}")
    text = f"Title: {title}\narXiv: {pid}\n\nAbstract:\n{abstract}"
    return Article(text, title, "arxiv.org", published, "en", False, "abstract")


def _hn_text(c: Candidate) -> Article:
    item_id = c.extra.get("hn_id")
    d = http_json(f"https://hn.algolia.com/api/v1/items/{item_id}")
    body = re.sub(r"<[^>]+>", " ", d.get("text") or "")
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) < 300:
        raise ExtractError("HN self-post too short")
    text, trunc = _cap(f"{d.get('title')}\n\n{body}")
    return Article(text, d.get("title") or c.title, "news.ycombinator.com",
                   to_local_date(d.get("created_at")), "en", trunc, "hn_text")


def _hf_model(c: Candidate, mid: str) -> Article:
    card = http_get(f"https://huggingface.co/{mid}/raw/main/README.md")
    card = re.sub(r"^---\n.*?\n---\n", "", card, flags=re.S)  # YAML メタデータを除く
    if len(card) < 500:
        raise ExtractError("model card too short")
    text, trunc = _cap(f"Model: {mid}\n\n{card}")
    return Article(text, mid, "huggingface.co", c.published, "en", trunc, "readme")


def _generic(c: Candidate) -> Article:
    if not robots_allowed(c.url):
        raise ExtractError(f"robots.txt disallows {c.url}")
    raw, _ = http_get_bytes(c.url, timeout=25, headers={"Accept": "text/html,application/xhtml+xml"})
    # バイト列のまま渡し、meta charset（Shift_JIS 等）の判定を trafilatura に任せる
    out = trafilatura.extract(raw, url=c.url, output_format="json", with_metadata=True,
                              include_comments=False, include_tables=True, favor_precision=True,
                              date_extraction_params={"extensive_search": False, "original_date": True})
    if not out:
        raise ExtractError("trafilatura returned nothing")
    d = json.loads(out)
    body = d.get("text") or ""
    if len(body) < 500:
        raise ExtractError(f"body too short ({len(body)} chars) — paywall / JS-rendered page?")
    if body.count("�") > 0.01 * len(body):
        raise ExtractError("too many undecodable characters (encoding problem)")
    title = d.get("title") or ""
    if not title or "�" in title:
        title = c.title  # 取得元のタイトル（RSS 等）を使う
    text, trunc = _cap(body)
    lang = "ja" if re.search(r"[぀-ヿ]", body[:2000]) else "en"
    return Article(text, title, d.get("sitename") or _site(c.url), (d.get("date") or "")[:10] or None,
                   lang, trunc, "article")


def fetch(c: Candidate) -> Article:
    host = _site(c.url)
    if c.extra.get("hn_id") and host == "news.ycombinator.com":
        return _hn_text(c)
    gh = github_parts(c.url)
    if gh is not None:
        return _github_repo(gh[0], gh[1]) if is_repo_top(gh) else _github_sub(gh)
    pid = _arxiv_id(c)
    if pid:
        return _paper(c, pid)
    if host == "huggingface.co" and c.source == "hf_models":
        return _hf_model(c, urlsplit(c.url).path.strip("/"))
    return _generic(c)
