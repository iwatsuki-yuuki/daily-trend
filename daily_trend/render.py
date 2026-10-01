"""表示: ターミナル用・Markdown 用・引用元テキスト。"""
from __future__ import annotations

import shutil
import unicodedata
from datetime import date, datetime

WEEKDAY = "月火水木金土日"


def _w(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


_NO_HEAD = set("。、，．）」』】〕！？：；ー…・%％")  # 行頭に来てはいけない文字


def wrap(text: str, width: int, indent: str = "") -> list[str]:
    """全角を幅2として折り返す。句読点・閉じ括弧は行頭に送らず、前の行にぶら下げる。"""
    lines, cur, cw = [], "", 0
    avail = max(width - len(indent), 10)
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur, cw = "", 0
            continue
        w = _w(ch)
        if cw + w > avail and not (ch in _NO_HEAD and cw + w <= avail + 2):
            lines.append(cur)
            cur, cw = "", 0
            if ch == " ":
                continue
        cur += ch
        cw += w
    if cur:
        lines.append(cur)
    return [indent + ln for ln in lines]


def _hhmm(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return iso


def _date_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{day}（{WEEKDAY[d.weekday()]}）"


def _ev_status(e: dict) -> str:
    if e.get("verified"):
        return "✓ 引用は本文に実在し、要点を裏付けている"
    if "found" not in e:
        return "✗ 根拠未確認"
    parts = []
    if not all(e.get("found") or [False]):
        parts.append("引用の一部が本文に無い")
    if e.get("supported") is False:
        parts.append(f"裏付けなし:「{e.get('unsupported_part')}」")
    elif e.get("supported") is None:
        parts.append("裏付け未判定")
    return "✗ " + " / ".join(parts)


def citations(rec: dict) -> list[str]:
    """引用元を Markdown の箇条書きで返す（C キー・trend cite と同じ形式）。"""
    src = rec["source"]
    pub = src.get("published_at") or "公開日不明"
    out = [f"- [{src.get('title') or src['url']}]({src['url']}) — {src.get('site') or ''}, {pub}"]
    for b in rec.get("buzz") or []:
        url = f" ({b['url']})" if b.get("url") else ""
        out.append(f"- {b['platform']}: {b['metric']}（{_hhmm(b.get('observed_at'))} 時点）{url}")
    return out


def to_markdown(rec: dict) -> str:
    L = [f"# {rec['headline']}", "", f"{_date_label(rec['date'])} ・ {rec.get('category') or ''}", "",
         f"**{rec['one_liner']}**", ""]
    ev = {e["point_index"]: e for e in rec.get("evidence") or []}
    for i, p in enumerate(rec.get("points") or []):
        mark = "" if ev.get(i, {}).get("verified", True) else " ⚠︎(根拠未確認)"
        L.append(f"- {p}{mark}")
    L += ["", f"**なぜ今**: {rec.get('why_now') or ''}", ""]
    if rec.get("terms"):
        L.append("用語（本文ではなく一般知識による説明）:")
    for t in rec.get("terms") or []:
        L.append(f"- *{t['term']}*: {t['explain']}")
    L += ["", "## 引用元", *citations(rec), ""]
    if rec.get("warnings"):
        L += ["## 注意", *[f"- {w}" for w in rec["warnings"]], ""]
    if rec.get("alternates"):
        L += ["## 次点"]
        for a in rec["alternates"]:
            L.append(f"- [{a['title']}]({a['url']}) — {a.get('reason') or ''}")
        L.append("")
    if rec.get("evidence"):
        L += ["## 要点の根拠（本文からの抜粋）"]
        for e in rec["evidence"]:
            L.append(f"{e['point_index'] + 1}. {_ev_status(e)}")
            for q, ok in zip(e.get("quotes") or [e["quote"]], e.get("found") or [e.get("verified")]):
                L.append(f"    - \"{q}\"{'' if ok else '（本文に見つからず）'}")
        L.append("")
    L.append(f"<sub>生成: {rec.get('generated_at')} / {rec.get('model')}</sub>")
    return "\n".join(L) + "\n"


def to_terminal(rec: dict, color: bool = True, show_evidence: bool = False) -> str:
    width = min(shutil.get_terminal_size((88, 24)).columns, 96) - 2
    B, D, C, Y, G, R = ("\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[32m", "\033[0m") if color else ("",) * 6
    ev = {e["point_index"]: e for e in rec.get("evidence") or []}
    out = [f"{D}{_date_label(rec['date'])} ・ {rec.get('category') or ''}{R}"]
    out += [f"{B}{ln}{R}" for ln in wrap("■ " + rec["headline"], width)]
    out += wrap(rec["one_liner"], width)
    out.append("")
    for i, p in enumerate(rec.get("points") or []):
        bad = not ev.get(i, {}).get("verified", True)
        lines = wrap(p + (" ⚠︎根拠未確認" if bad else ""), width, "   ")
        lines[0] = " ・" + lines[0][3:]
        out += [f"{Y}{ln}{R}" if bad else ln for ln in lines]
    out.append("")
    out += wrap(f"なぜ今: {rec.get('why_now') or ''}", width)
    for t in rec.get("terms") or []:
        out += [f"{D}{ln}{R}" for ln in wrap(f"用語 {t['term']}: {t['explain']}（一般知識）", width)]
    out.append(f"{D}{'─' * 3} 引用元 {'─' * max(width - 12, 3)}{R}")
    src = rec["source"]
    out += wrap(f"元記事: {src.get('title') or ''} — {src.get('site') or ''}（公開 {src.get('published_at') or '不明'}）", width)
    out.append(f"        {C}{src['url']}{R}")
    for b in rec.get("buzz") or []:
        out += wrap(f"話題元: {b['platform']} {b['metric']}（{_hhmm(b.get('observed_at'))} 時点）", width)
        if b.get("url"):
            out.append(f"        {C}{b['url']}{R}")
    for w in rec.get("warnings") or []:
        out += [f"{Y}{ln}{R}" for ln in wrap(f"注意: {w}", width)]
    if show_evidence and rec.get("evidence"):
        out.append(f"{D}{'─' * 3} 要点の根拠（本文からの抜粋） {'─' * max(width - 30, 3)}{R}")
        for e in rec["evidence"]:
            head = f"{e['point_index'] + 1}. {_ev_status(e)}"
            out.append(f"{G}{head}{R}" if e.get("verified") else f"{Y}{head}{R}")
            for q, ok in zip(e.get("quotes") or [e["quote"]], e.get("found") or [e.get("verified")]):
                out += [f"{D}{ln}{R}" if ok else f"{Y}{ln}{R}" for ln in wrap(f"\"{q}\"" + ("" if ok else " ✗本文に無し"), width, "   ")]
    out.append(f"{D}trend open: 元記事 ・ trend buzz: 話題元 ・ trend -e: 根拠 ・ trend ls: 過去分{R}")
    return "\n".join(out)
