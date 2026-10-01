"""trend コマンド。

  trend              最新の1件を表示（-e で要点の根拠も表示）
  trend 2026-09-27   指定日の1件を表示（-1 で前日、-2 で2日前）
  trend open         元記事をブラウザで開く
  trend buzz         話題元（HN / はてブ など）を開く
  trend cite         引用元を Markdown で表示（クリップボードにもコピー）
  trend ls           過去の見出し一覧
  trend run          今日の1件を生成（--force で別の記事に作り直し、--if-missing で未生成時のみ）
  trend path         データの場所を表示
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import subprocess
import sys
import traceback
from datetime import date, timedelta

from . import extract, rank, render, sources, store, summarize
from .common import DATA, DAYS, LOGS, load_config, now_local, today_str

log = logging.getLogger("daily_trend")

# 別の trend run が実行中で何もしなかったときの終了コード（EX_TEMPFAIL）。
# 0 を返すと TrendBar の「今すぐ生成」が「成功」と表示してしまうので、0 以外にする。
EXIT_BUSY = 75

MAX_REPAIRS = 2  # 裏付けの無い箇所の書き直しは最大2回（1回あたり約 $0.03）


def _setup_logging(verbose: bool) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOGS / f"{today_str()}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def _resolve_day(arg: str | None) -> dict | None:
    if not arg:
        return store.latest()
    if arg.lstrip("-").isdigit() and arg.startswith("-"):
        days = store.list_days()
        k = int(arg)  # -1 = 最新の1つ前
        return store.load_day(days[k - 1]) if len(days) >= 1 - k else None
    return store.load_day(arg)


def _needs_retry(evidence: list[dict], head: dict) -> bool:
    return bool(head["unsupported"]) or not all(e["verified"] for e in evidence)


def _quality(evidence: list[dict], head: dict) -> tuple:
    """書き直し前後のどちらを採るか。判定できなかった版は、裏付けなしの版より下に置く。"""
    return (head["judged"], sum(e["verified"] for e in evidence), -len(head["unsupported"]))


def _feedback(evidence: list[dict], head: dict) -> str:
    fb = []
    for e in evidence:
        n = e["point_index"] + 1
        if not all(e["found"]):
            fb.append(f"要点{n}: quotes の一部が本文中に見つかりません。本文からそのまま（翻訳せず、省略記号も使わず）連続した一節を引用し直してください。")
        if e["supported"] is False:
            fb.append(f"要点{n}: 「{e['unsupported_part']}」が quotes で裏付けられていません。裏付ける quote を足すか、その部分を削ってください。")
    for h in head["unsupported"]:
        fb.append(f"{h}に、quotes と書誌情報で裏付けられない内容があります。要点に書いた内容だけで書き直してください。")
    return "\n".join(fb)


def build_record(day: str, c, art, summ: dict, evidence: list[dict], model: str, alternates: list[dict], observed_at: str,
                 head: dict | None = None) -> dict:
    head = head or {"judged": True, "unsupported": []}
    warnings = []
    for e in evidence:
        n = e["point_index"] + 1
        if not all(e.get("found") or [False]):
            warnings.append(f"要点{n}: 根拠の引用の一部が本文中に見つかりませんでした")
        if e.get("supported") is False:
            warnings.append(f"要点{n}: 「{e.get('unsupported_part')}」は引用で裏付けられていません")
        elif e.get("supported") is None:
            warnings.append(f"要点{n}: 裏付けの判定ができませんでした")
    if not head["judged"]:
        warnings.append("見出し・結論の1文・「なぜ今」は、裏付けの判定ができませんでした")
    for h in head["unsupported"]:
        warnings.append(f"{h}に引用で裏付けられない内容が含まれる可能性があります")
    if art.truncated:
        warnings.append("本文が長いため先頭 60,000 字だけを要約に使いました")
    if art.kind == "abstract":
        warnings.append("論文の要旨（abstract）だけを根拠にした要約です")
    if art.kind == "readme":
        warnings.append("リポジトリの README だけを根拠にした要約です")
    return {
        "schema_version": 1,
        "date": day,
        "generated_at": now_local().isoformat(timespec="seconds"),
        "model": model,
        "category": summ["category"],
        "headline": summ["headline"],
        "one_liner": summ["one_liner"],
        "points": [p["text"] for p in summ["points"]],
        "why_now": summ["why_now"],
        "terms": summ["terms"],
        "source": {
            "title": art.title or c.title,
            "url": c.url,
            "site": art.site,
            "published_at": art.published_at,
            "lang": art.lang,
        },
        "buzz": [{"platform": b.platform, "url": b.url, "metric": b.metric, "observed_at": observed_at} for b in c.buzz],
        "evidence": evidence,
        # 見出し・結論・「なぜ今」の判定結果（ok / unsupported / unjudged）。TrendBar は warnings の文言ではなくこれで ⚠︎ を付ける
        "field_checks": {
            key: ("unjudged" if not head["judged"] else "unsupported" if name in head["unsupported"] else "ok")
            for key, name in (("headline", "見出し"), ("one_liner", "結論の1文"), ("why_now", "「なぜ今」"))
        },
        "warnings": warnings,
        "alternates": alternates,
    }


def run(force: bool, if_missing: bool, dry_run: bool) -> int:
    day = today_str()
    if (if_missing or not force) and (DAYS / f"{day}.json").exists():
        log.info("already generated for %s", day)
        if not if_missing:
            print(f"{day} は生成済みです。作り直すときは trend run --force")
        return 0
    if if_missing:
        # 自動実行（launchd）は失敗しても毎時再試行するので、API を呼びすぎないよう1日3回までにする
        attempts = LOGS / f"attempts-{day}"
        n = int(attempts.read_text()) if attempts.exists() else 0
        if n >= 3:
            log.info("skip: already attempted %d times today", n)
            return 0
    cfg = load_config()
    pick_cfg = cfg.get("pick", {})
    model = pick_cfg.get("model", "claude-opus-5")
    observed_at = now_local().isoformat(timespec="seconds")

    cands, errors = sources.fetch_all(cfg.get("sources", {}))
    for k, e in errors.items():
        log.warning("source %s failed: %s", k, e)
    log.info("fetched %d candidates from %d sources", len(cands), len({c.source for c in cands}))
    # 作り直し（--force）のときは、今日すでに出した記事も既出として除き、別の記事を選ぶ
    shown_today = store.shown(store.load_day(day))
    hist = store.history(day, int(pick_cfg.get("dedupe_days", 30))) + shown_today
    short, dropped = rank.shortlist(cands, hist, int(pick_cfg.get("max_candidates", 40)))
    store.save_candidates(day, {"observed_at": observed_at, "errors": errors, "dropped_as_seen": dropped,
                                "shortlist": [c.to_dict() for c in short]})
    if not short:
        log.error("no candidates")
        print("候補が0件でした。ネットワークか取得元を確認してください。", file=sys.stderr)
        return 1

    if if_missing:
        attempts.write_text(str(n + 1))  # ネットワーク取得まで成功し、API を呼ぶ回だけ数える
    picks = summarize.select(short, pick_cfg.get("interests", ""), model,
                             avoid=[h["title"] for h in shown_today if h.get("title")])
    log.info("picks: %s", [(p["id"], short[p["id"]].title) for p in picks])

    for p in picks:
        c = short[p["id"]]
        # 1件ごとの失敗（本文が取れない・要約 API のエラーや拒否）は次の候補へ進む
        try:
            art = extract.fetch(c)
            summ = summarize.summarize(c, art, model)
            evidence, head = summarize.verify(summ, art, model)
        except Exception as e:
            log.warning("pick failed for %s: %s", c.url, e, exc_info=True)
            continue
        for attempt in range(MAX_REPAIRS):  # 裏付けの無い箇所があれば、その箇所だけ直させる
            if not _needs_retry(evidence, head):
                break
            fb = _feedback(evidence, head)
            log.info("verification failed; repair %d: %s", attempt + 1, fb)
            try:
                summ2 = summarize.summarize(c, art, model, retry_feedback=fb, previous=summ)
                ev2, head2 = summarize.verify(summ2, art, model)
            except Exception as e:  # 書き直しに失敗しても、それまでの要約は使う
                log.warning("repair failed for %s: %s", c.url, e, exc_info=True)
                break
            if _quality(ev2, head2) >= _quality(evidence, head):
                summ, evidence, head = summ2, ev2, head2
        alternates = [{"title": short[q["id"]].title, "url": short[q["id"]].url,
                       "buzz_url": next((b.url for b in short[q["id"]].buzz if b.url), None),
                       "reason": q["reason"]} for q in picks if q is not p]
        rec = build_record(day, c, art, summ, evidence, model, alternates, observed_at, head)
        rec["pick_reason"] = p["reason"]
        if shown_today:
            rec["replaced"] = shown_today  # 翌日以降の重複除外にも使う（store.history）
        if dry_run:
            print(render.to_terminal(rec, show_evidence=True))
            return 0
        path = store.save_day(rec, render.to_markdown(rec))
        log.info("saved %s (%s)", path, rec["headline"])
        print(render.to_terminal(rec))
        return 0

    log.error("all picks failed")
    print("選んだ候補の本文取得または要約にすべて失敗しました。ログ: " + str(LOGS), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trend", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default=None)
    ap.add_argument("-e", "--evidence", action="store_true", help="要点の根拠（本文からの抜粋）も表示")
    ap.add_argument("-n", type=int, default=14, help="ls で表示する件数")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--if-missing", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="保存せず表示だけ")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args, rest = ap.parse_known_args(argv)
    # "trend -1" のような負の数は argparse がオプション扱いするので拾い直す
    if args.cmd is None and rest and rest[0].lstrip("-").isdigit():
        args.cmd = rest[0]
    color = sys.stdout.isatty() and not args.no_color

    if args.cmd == "run":
        _setup_logging(args.verbose)
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / ".run.lock", "w") as lockf:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("別の trend run が実行中です。", file=sys.stderr)
                return EXIT_BUSY
            try:
                return run(args.force, args.if_missing, args.dry_run)
            except Exception:
                log.error("run failed:\n%s", traceback.format_exc())
                print("生成に失敗しました。詳細: " + str(LOGS / f"{today_str()}.log"), file=sys.stderr)
                return 1

    if args.cmd == "path":
        print(DATA)
        return 0

    if args.cmd == "ls":
        for d in reversed(store.list_days()[-args.n:]):
            rec = store.load_day(d) or {}
            print(f"{d}  {rec.get('headline', '?')}")
        return 0

    if args.cmd in ("open", "buzz", "cite"):
        rec = store.latest()
        if not rec:
            print("まだ1件もありません。trend run で生成してください。", file=sys.stderr)
            return 1
        if args.cmd == "open":
            subprocess.run(["open", rec["source"]["url"]])
        elif args.cmd == "buzz":
            url = next((b["url"] for b in rec.get("buzz") or [] if b.get("url")), None)
            if not url:
                print("話題元の URL がありません。", file=sys.stderr)
                return 1
            subprocess.run(["open", url])
        else:
            text = "\n".join(render.citations(rec))
            print(text)
            subprocess.run(["pbcopy"], input=text.encode())
        return 0

    rec = _resolve_day(args.cmd)
    if not rec:
        if args.cmd:
            print(f"{args.cmd} の記録はありません。trend ls で一覧を確認できます。", file=sys.stderr)
        else:
            print("まだ1件もありません。trend run で生成してください（初回は1分ほどかかります）。", file=sys.stderr)
        return 1
    print(render.to_terminal(rec, color=color, show_evidence=args.evidence))
    if rec["date"] != today_str() and not args.cmd:
        yesterday = (date.fromisoformat(today_str()) - timedelta(days=1)).isoformat()
        note = "前日分" if rec["date"] == yesterday else f"{rec['date']} 分"
        print(f"（今日の分はまだ生成されていません。表示中は{note}です）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
