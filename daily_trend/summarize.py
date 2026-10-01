"""Claude で「1件を選ぶ」と「1分要約を書く」。引用 URL は LLM に書かせず、コードが候補データから組み立てる。"""
from __future__ import annotations

import json
import re
import unicodedata

import logging

import anthropic

from .common import Candidate
from .extract import Article
from .rank import KINDS

STYLE = """文体の規則（必ず守る）:
- 結論を先に書く。一文を短くし、一文に一つのことだけ書く。
- 比喩・例え話・文学的な言い回しを使わない。「まさに」「いわば」「本質的に」「革命的」などの飾りや強調の言葉を使わない。
- 専門用語は必要なときだけ使う。説明が要る用語は terms に入れる。
- 本文に書かれていないことは書かない。推測で数値や固有名詞を補わない。"""

CATEGORIES = ["AI/ML", "開発ツール", "インフラ・クラウド", "セキュリティ", "ハードウェア", "科学技術", "その他"]

SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "description": "推す順に3件",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string", "description": "選んだ理由を日本語で1文（40字以内）。数値は書かない"},
                },
                "required": ["id", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["picks"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "日本語の見出し。35字以内。何が起きたかを具体的に"},
        "one_liner": {"type": "string", "description": "結論を1文。60字以内"},
        "category": {"type": "string", "enum": CATEGORIES},
        "points": {
            "type": "array",
            "description": "要点をちょうど3つ（4つ以上にしない）。重要な順",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要点。日本語で1〜2文、70字以内"},
                    "quotes": {
                        "type": "array",
                        "description": "この要点の根拠となる本文の一節を原文のまま（翻訳・言い換え・省略記号の使用は禁止）。各 10〜30 語の連続した文字列を1〜3個。"
                                       "要点に書いたすべての主張（数値・固有名詞・因果）が、どれかの quote に書かれていること",
                        "items": {"type": "string"},
                    },
                },
                "required": ["text", "quotes"],
                "additionalProperties": False,
            },
        },
        "why_now": {"type": "string", "description": "なぜ今話題なのか（発表・公開・リリース等）。本文と書誌情報から分かる範囲で1文。"
                                                      "話題元の数値（points 等）は書かない。分からなければ「本文からは不明」"},
        "terms": {
            "type": "array",
            "description": "初見では分かりにくい専門用語 0〜2 個",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "explain": {"type": "string", "description": "40字以内のひとこと説明"},
                },
                "required": ["term", "explain"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "one_liner", "category", "points", "why_now", "terms"],
    "additionalProperties": False,
}


log = logging.getLogger("daily_trend")


class LLMError(Exception):
    pass


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=3, timeout=180)


def _call(model: str, system: str, user: str, schema: dict, effort: str) -> dict:
    kwargs: dict = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if not model.startswith("claude-haiku"):
        kwargs["output_config"]["effort"] = effort
    if model.startswith(("claude-opus-5", "claude-fable-5")):
        # 安全分類器による拒否時は、サーバー側で推奨モデルに自動で振り替える
        resp = _client().beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = _client().messages.create(**kwargs)
    u = resp.usage
    log.info("api %s effort=%s in=%s out=%s cache_read=%s", resp.model, effort, u.input_tokens, u.output_tokens,
             getattr(u, "cache_read_input_tokens", None))
    if resp.stop_reason == "refusal":
        raise LLMError(f"refused: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise LLMError("hit max_tokens")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError(f"no text block (stop_reason={resp.stop_reason})")
    return json.loads(text)


def _cand_line(i: int, c: Candidate, show_kind: bool = False) -> str:
    buzz = "; ".join(f"{b.platform} {b.metric}" for b in c.buzz)
    desc = re.sub(r"\s+", " ", c.description)[:220]
    kind = f"\n    種類: {KINDS.get(c.extra.get('kind'), '不明')}" if show_kind else ""
    return f"[{i}] {c.title}\n    site: {re.sub(r'^https?://', '', c.url)[:80]}{kind}\n    話題: {buzz}\n    説明: {desc}"


def select(cands: list[Candidate], interests: str, model: str, avoid: list[str] | None = None,
           focus_hint: str | None = None) -> list[dict]:
    """avoid: 最近すでに出した記事の題名（今日の作り直し前の分を含む）。同じ話題の別記事を選ばせないために渡す。
    focus_hint: 種類を絞った・重みを付けたときの追加の指示（rank.focus_hint）。"""
    listing = "\n".join(_cand_line(i, c, show_kind=bool(focus_hint)) for i, c in enumerate(cands))
    system = ("あなたは技術ニュースの編集者です。候補の中から、読者が今日1分で読む価値が最も高い技術トピックを選びます。\n"
              "選ぶ基準: (1) 読者の関心の優先順位に合う (2) 今日実際に話題になっている（数値が大きい・複数の場で話題）"
              " (3) 何が新しいのかが具体的（発表・公開・論文・リリース）。\n"
              "同じ話題（同じ製品名・技術名）が複数の候補に出ている場合、それは今日広く話題になっている強い根拠として扱う。"
              "その場合は、その話題の一次情報に最も近い候補（公式発表・本人の解説・論文）を選ぶ。\n"
              "元記事の本文を取得できない可能性があるので、3件を推す順に返す。")
    user = f"# 読者の関心（上ほど優先）\n{interests.strip()}\n\n# 候補（{len(cands)}件）\n{listing}"
    if focus_hint:
        system += "\n" + focus_hint
    if avoid:
        system += ("\n読者は「最近すでに出した記事」を読んでいて、別の話題を求めている。"
                   "それと同じ話題（同じ製品・同じ発表・同じ出来事）の候補は選ばない。")
        user += "\n\n# 最近すでに出した記事\n" + "\n".join(f"- {t}" for t in avoid)
    out = _call(model, system, user, SELECT_SCHEMA, effort="low")
    seen, picks = set(), []
    for p in out["picks"]:
        if 0 <= p["id"] < len(cands) and p["id"] not in seen:
            seen.add(p["id"])
            picks.append(p)
    if not picks:
        raise LLMError("select returned no valid ids")
    return picks


_PUNCT = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "})
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def _norm(s: str, drop_space: bool) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_PUNCT).lower()
    s = re.sub(r"[*`]", "", s)  # Markdown の強調記号は本文側・引用側で揺れるので除く
    return re.sub(r"\s+", "" if drop_space else " ", s).strip()


_ALNUM = re.compile(r"[0-9a-z]")


def _boundary_ok(t: str, i: int, j: int) -> bool:
    """t[i:j] が単語・数値の途中から始まったり途中で終わったりしていないか（英数字の端だけ確認する）。"""
    first, last = t[i], t[j - 1]
    if _ALNUM.match(first) and i > 0:
        if _ALNUM.match(t[i - 1]):
            return False
        if first.isdigit() and t[i - 1] == "." and i > 1 and t[i - 2].isdigit():  # 12.4 の 4 から始まる
            return False
    if _ALNUM.match(last) and j < len(t):
        if _ALNUM.match(t[j]):
            return False
        if last.isdigit() and t[j] == "." and j + 1 < len(t) and t[j + 1].isdigit():  # 12.4 の 12 で終わる
            return False
    return True


def quote_in_text(quote: str, text: str) -> bool:
    """根拠の引用が本文に実在するか。正規化後の完全一致で判定する。
    - あいまい一致はしない（数値の取り違えを通さないため）。
    - 省略記号（… / ...）を含む引用は不合格（途中の否定語や順序を飛ばせないようにする）。
    - 英数字で始まる・終わる引用は、本文側で単語や数値の途中になっていないかを確認する（15% の 5% など）。
    - 日本語を含む引用は空白を無視して比べる。"""
    drop = bool(_CJK.search(quote))
    q = _norm(quote, drop).strip(" \"'「」『』")
    if "..." in q or len(q) < 12:
        return False
    t = _norm(text, drop)
    return any(_boundary_ok(t, m.start(), m.start() + len(q)) for m in re.finditer(re.escape(q), t))


def summarize(c: Candidate, art: Article, model: str, retry_feedback: str | None = None, previous: dict | None = None) -> dict:
    out = _summarize(c, art, model, retry_feedback, previous)
    out["points"] = out["points"][:3]  # 1分で読める量に固定する（重要な順に並べさせている）
    return out


def _summarize(c: Candidate, art: Article, model: str, retry_feedback: str | None = None, previous: dict | None = None) -> dict:
    buzz = "、".join(dict.fromkeys(b.platform for b in c.buzz))
    system = ("あなたは技術ニュースを日本語で1分（400字前後）に要約する編集者です。"
              "与えられた本文だけを根拠にします。\n\n" + STYLE)
    user = (f"# 記事\nタイトル: {art.title}\nサイト: {art.site}\n公開日: {art.published_at or '不明'}\n"
            f"種類: {art.kind}{'（本文は長いため先頭 60,000 字のみ）' if art.truncated else ''}\n\n"
            f"# この記事が話題になっている場所（参考）\n{buzz}\n\n"
            f"# 本文\n<<<\n{art.text}\n>>>")
    if retry_feedback:
        if previous is not None:
            # 全体を作り直すと、前回は正しかった部分が崩れることがあるので、指摘の箇所だけ直させる
            user += ("\n\n# 前回の出力\n" + json.dumps(previous, ensure_ascii=False, indent=1) +
                     "\n\n# 前回の出力への指摘\n" + retry_feedback +
                     "\n\n指摘された箇所だけを直し、それ以外（指摘の無い要点と、その quotes）は前回の出力をそのまま使ってください。"
                     "直すときは、主張を quotes に書かれている範囲まで狭めるか、主張を裏付ける quote を本文から足してください。")
        else:
            user += f"\n\n# 前回の出力への指摘\n{retry_feedback}"
    return _call(model, system, user, SUMMARY_SCHEMA, effort="medium")


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "supported": {"type": "boolean"},
                    "unsupported_part": {"type": "string", "description": "裏付けのない部分。無ければ空文字"},
                },
                "required": ["index", "supported", "unsupported_part"],
                "additionalProperties": False,
            },
        },
        "headline_supported": {"type": "boolean"},
        "one_liner_supported": {"type": "boolean"},
        "why_now_supported": {"type": "boolean"},
    },
    "required": ["points", "headline_supported", "one_liner_supported", "why_now_supported"],
    "additionalProperties": False,
}


def judge(summary: dict, model: str, art: Article) -> dict:
    """各要点の中身が、付けた引用だけで裏付けられるかを判定する（本文全体は渡さない）。"""
    lines = []
    for i, p in enumerate(summary["points"]):
        qs = "\n".join(f"    > {q}" for q in p["quotes"])
        lines.append(f"[{i}] 要点: {p['text']}\n  引用:\n{qs}")
    all_q = "\n".join(f"> {q}" for p in summary["points"] for q in p["quotes"])
    system = ("あなたは要約の事実確認係です。要約文の主張が、示された引用だけで裏付けられるかを厳しく判定します。"
              "引用に書かれていない数値・固有名詞・因果関係・評価が1つでも含まれていれば supported=false にし、その部分を unsupported_part に書きます。"
              "日本語への翻訳・言い換え・語順の違いは問題にしません。"
              "書誌情報から分かること（誰が・どこで公開したか）や、「公開した」「発表した」のように記事の存在自体から言えることは裏付けありとします。")
    user = (f"# 記事の書誌情報（これも根拠として使ってよい）\nタイトル: {art.title}\nサイト: {art.site}\n公開日: {art.published_at or '不明'}\n種類: {art.kind}\n\n"
            "# 要点と引用\n" + "\n".join(lines) +
            f"\n\n# 見出し・結論・なぜ今（すべての引用と書誌情報を根拠にしてよい）\n見出し: {summary['headline']}\n"
            f"結論: {summary['one_liner']}\nなぜ今: {summary['why_now']}\n引用:\n{all_q}")
    return _call(model, system, user, JUDGE_SCHEMA, effort="low")


def verify(summary: dict, art: Article, model: str) -> tuple[list[dict], dict]:
    """(1) 引用が本文に実在するか（コードで照合） (2) 各文が引用で裏付けられるか（LLM で判定）。
    返り値の head = {"judged": 判定できたか, "unsupported": 裏付けのない欄の名前}。"""
    try:
        j = judge(summary, model, art)
        support = {p["index"]: p for p in j["points"]}
        head = {"judged": True, "unsupported": [name for ok, name in (
            (j["headline_supported"], "見出し"), (j["one_liner_supported"], "結論の1文"), (j["why_now_supported"], "「なぜ今」"))
            if not ok]}
    except Exception as e:  # 判定が失敗しても要約は捨てない（未判定として記録）
        log.warning("judge failed: %s", e)
        support, head = {}, {"judged": False, "unsupported": []}
    evidence = []
    for i, p in enumerate(summary["points"]):
        found = [quote_in_text(q, art.text) for q in p["quotes"]]
        sup = support.get(i, {"supported": None, "unsupported_part": ""})
        evidence.append({
            "point_index": i,
            "quote": " / ".join(p["quotes"]),
            "quotes": p["quotes"],
            "found": found,
            "supported": sup["supported"],
            "unsupported_part": sup["unsupported_part"],
            "verified": bool(found) and all(found) and sup["supported"] is True,
        })
    return evidence, head
