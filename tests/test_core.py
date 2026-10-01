"""ネットワーク・API を使わない単体テスト。 実行: .venv/bin/python -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DAILY_TREND_HOME"] = tempfile.mkdtemp()  # data/ をテスト用の一時フォルダに向ける

from daily_trend import cli, rank, render, store, summarize  # noqa: E402
from daily_trend.common import Buzz, Candidate  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/2026-09-27.json").read_text())


class RankTest(unittest.TestCase):
    def test_canonical_url(self):
        self.assertEqual(rank.canonical_url("https://www.Example.com/a/?utm_source=x&id=3#f"), "//example.com/a?id=3")
        self.assertEqual(rank.canonical_url("https://arxiv.org/pdf/2509.01234v2"), "arxiv:2509.01234")
        self.assertEqual(rank.canonical_url("https://huggingface.co/papers/2509.01234"), "arxiv:2509.01234")
        self.assertEqual(rank.canonical_url("https://github.com/Foo/Bar/tree/main"), "github:foo/bar")
        # issue・PR はリポジトリ本体と別の記事として扱う
        self.assertNotEqual(rank.canonical_url("https://github.com/python/cpython/issues/1"), "github:python/cpython")
        self.assertNotEqual(rank.canonical_url("https://github.com/python/cpython/pull/1"),
                            rank.canonical_url("https://github.com/python/cpython/issues/2"))
        # 値の無いクエリ・ハッシュルーティングで別ページを同一視しない
        self.assertNotEqual(rank.canonical_url("https://ex.jp/wiki/?PageA"), rank.canonical_url("https://ex.jp/wiki/?PageB"))
        self.assertNotEqual(rank.canonical_url("https://ex.org/#/post/1"), rank.canonical_url("https://ex.org/#/post/2"))

    def test_denied_hosts_are_dropped(self):
        a = Candidate("hackernews", "r", "https://www.reddit.com/r/x/comments/1/", 999)
        b = Candidate("hackernews", "ok", "https://example.com/a", 1)
        short, _ = rank.shortlist([a, b], [], 10)
        self.assertEqual([c.title for c in short], ["ok"])

    def test_merge_across_sources_and_history(self):
        a = Candidate("hackernews", "Show HN: Foo", "https://example.com/foo?utm_medium=a", 500,
                      [Buzz("Hacker News", "https://news.ycombinator.com/item?id=1", "500 points")])
        b = Candidate("hatena", "Foo の紹介", "https://www.example.com/foo", 300,
                      [Buzz("はてなブックマーク", "https://b.hatena.ne.jp/entry/s/example.com/foo", "300 users")])
        c = Candidate("hackernews", "Other", "https://other.com/x", 100, [Buzz("Hacker News", None, "100 points")])
        short, dropped = rank.shortlist([a, b, c], [{"url": "https://other.com/x", "title": "Other"}], 10)
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0].extra["sources"], ["hackernews", "hatena"])
        self.assertEqual({x.platform for x in short[0].buzz}, {"Hacker News", "はてなブックマーク"})
        self.assertEqual(dropped, ["Other"])

    def test_same_family_and_listing_only_sources_get_no_bonus(self):
        def top(extra_source, url="https://github.com/o/r"):
            cands = [Candidate("hackernews", "solo", "https://solo.com", 100), Candidate("github", "repo", "https://github.com/o/r", 100),
                     Candidate(extra_source, "again", url, 100)]
            return rank.shortlist(cands, [], 10)[0][0].title
        self.assertEqual(top("hatena"), "repo")            # 別の場（はてブ）にも出ていれば加点
        self.assertEqual(top("github_trending"), "solo")   # GitHub の2つの取得元は1つと数える
        post = [Candidate("hackernews", "solo", "https://solo.com", 100), Candidate("hatena", "post", "https://blog.example/p", 100),
                Candidate("techblogs", "post", "https://blog.example/p", 1)]
        self.assertEqual(rank.shortlist(post, [], 10)[0][0].title, "solo")  # フィードに載っているだけでは加点しない

    def test_each_source_gets_similar_slots(self):
        big = [Candidate("hf_papers", f"p{i}", f"https://arxiv.org/abs/2609.{10000 + i}", 100 - i) for i in range(90)]
        small = [Candidate("techmeme", f"t{i}", f"https://t{i}.com", 100 - i) for i in range(8)]
        short, _ = rank.shortlist(big + small, [], 12)
        self.assertEqual(sum(c.source == "techmeme" for c in short), 6)  # 件数の多い取得元が枠を占めない

    def test_multi_source_scores_higher(self):
        solo = [Candidate("hackernews", f"t{i}", f"https://s{i}.com", 100 - i) for i in range(5)]
        dup_hn = Candidate("hackernews", "dup", "https://dup.com", 1)
        dup_ht = Candidate("hatena", "dup ja", "https://dup.com/", 50)
        short, _ = rank.shortlist(solo + [dup_hn, dup_ht], [], 10)
        self.assertEqual(short[0].title, "dup")


class QuoteTest(unittest.TestCase):
    TEXT = "Intro. The model runs 2.1x faster on one GPU, according to “our tests”.\nEnd."

    def test_exact_and_whitespace(self):
        self.assertTrue(summarize.quote_in_text("The model runs  2.1x faster on one GPU", self.TEXT))

    def test_curly_quotes(self):
        self.assertTrue(summarize.quote_in_text('according to "our tests"', self.TEXT))

    def test_invented(self):
        self.assertFalse(summarize.quote_in_text("the model runs 3x faster on two GPUs", self.TEXT))

    def test_changed_number_is_rejected(self):
        long_text = "We evaluate on 14 benchmarks and the model runs 2.1x faster on one GPU than the baseline system."
        self.assertTrue(summarize.quote_in_text(long_text, long_text))
        self.assertFalse(summarize.quote_in_text(long_text.replace("2.1x", "3.1x"), long_text))

    def test_ellipsis_is_rejected(self):
        self.assertFalse(summarize.quote_in_text("The model runs 2.1x faster … according to “our tests”", self.TEXT))

    def test_partial_numbers_and_words_are_rejected(self):
        t = ("Our method yields a 15% improvement on MMLU and costs $1.5 million to train. "
             "The model is not yet supported on macOS. Windows support ships in version 12.4 next month.")
        self.assertTrue(summarize.quote_in_text("a 15% improvement on MMLU", t))
        self.assertFalse(summarize.quote_in_text("5% improvement on MMLU", t))          # 15% の途中から
        self.assertFalse(summarize.quote_in_text("support ships in version 12", t))    # 12.4 の途中まで
        self.assertFalse(summarize.quote_in_text("ships in version 12.4 next mont", t))  # 単語の途中まで
        self.assertFalse(summarize.quote_in_text("The model is supported on macOS", t))  # not yet を省略

    def test_japanese_ignores_spaces(self):
        ja = "Jevに投げられる質問の型は\n3つに絞られています。 型が保証されます。性能は15倍に向上した。"
        self.assertTrue(summarize.quote_in_text("Jevに投げられる質問の型は3つに絞られています。", ja))
        self.assertFalse(summarize.quote_in_text("Jevに投げられる質問の型は4つに絞られています。", ja))
        self.assertFalse(summarize.quote_in_text("5倍に向上した。", ja + "追加の文章です"))

    def test_too_short(self):
        self.assertFalse(summarize.quote_in_text("GPU", self.TEXT))


class RenderTest(unittest.TestCase):
    def test_wrap_width_and_kinsoku(self):
        lines = render.wrap("あいうえおかきくけこさしすせそたちつてと。なにぬ", 40)
        self.assertEqual(lines[0][-1], "。")  # 句点を行頭に送らない
        for ln in render.wrap("あ" * 100, 40):
            self.assertLessEqual(sum(render._w(ch) for ch in ln), 40)

    def test_terminal_and_markdown_contain_citations(self):
        t = render.to_terminal(FIXTURE, color=False)
        md = render.to_markdown(FIXTURE)
        for out in (t, md):
            self.assertIn(FIXTURE["source"]["url"], out)
            for b in FIXTURE["buzz"]:
                self.assertIn(b["url"], out)

    def test_citations_format(self):
        c = render.citations(FIXTURE)
        self.assertTrue(c[0].startswith("- [Dummy article title"))
        self.assertIn("07:00 時点", c[1])


class RecordTest(unittest.TestCase):
    def test_build_record_warnings_and_store(self):
        from daily_trend.extract import Article
        cand = Candidate("hackernews", "T", "https://ex.com/a", 10, [Buzz("Hacker News", "https://news.ycombinator.com/item?id=9", "10 points")])
        art = Article("body", "Title", "ex.com", "2026-09-27", "en", True, "article")
        summ = {"category": "AI/ML", "headline": "見出し", "one_liner": "結論。", "why_now": "理由。", "terms": [],
                "points": [{"text": "a", "quotes": ["q1"]}, {"text": "b", "quotes": ["q2"]}, {"text": "c", "quotes": ["q3"]}]}
        ev = [
            {"point_index": 0, "quote": "q1", "quotes": ["q1"], "found": [True], "supported": True, "unsupported_part": "", "verified": True},
            {"point_index": 1, "quote": "q2", "quotes": ["q2"], "found": [False], "supported": True, "unsupported_part": "", "verified": False},
            {"point_index": 2, "quote": "q3", "quotes": ["q3"], "found": [True], "supported": False, "unsupported_part": "2倍", "verified": False},
        ]
        rec = cli.build_record("2026-09-28", cand, art, summ, ev, "m", [], "2026-09-28T07:00:00+09:00",
                               {"judged": True, "unsupported": ["見出し"]})
        w = "\n".join(rec["warnings"])
        self.assertIn("要点2", w)
        self.assertIn("「2倍」", w)
        self.assertIn("先頭 60,000 字", w)
        self.assertIn("見出し", w)
        self.assertEqual(rec["points"], ["a", "b", "c"])
        store.save_day(rec, render.to_markdown(rec))
        self.assertEqual(store.latest()["headline"], "見出し")
        self.assertEqual(store.history("2026-09-29", 30)[0]["url"], "https://ex.com/a")
        self.assertEqual(store.history("2026-09-28", 30), [])  # 当日分は含めない

    def test_unjudged_is_warned_and_ranked_lower(self):
        from daily_trend.extract import Article
        cand = Candidate("hackernews", "T", "https://ex.com/a", 10)
        art = Article("body", "Title", "ex.com", None, "en", False, "article")
        summ = {"category": "AI/ML", "headline": "h", "one_liner": "o", "why_now": "w", "terms": [], "points": []}
        rec = cli.build_record("2026-09-28", cand, art, summ, [], "m", [], "t", {"judged": False, "unsupported": []})
        self.assertIn("判定ができませんでした", "\n".join(rec["warnings"]))
        self.assertEqual(rec["field_checks"], {"headline": "unjudged", "one_liner": "unjudged", "why_now": "unjudged"})
        rec2 = cli.build_record("2026-09-28", cand, art, summ, [], "m", [], "t", {"judged": True, "unsupported": ["「なぜ今」"]})
        self.assertEqual(rec2["field_checks"], {"headline": "ok", "one_liner": "ok", "why_now": "unsupported"})
        ok = [{"verified": True}]
        self.assertLess(cli._quality(ok, {"judged": False, "unsupported": []}),
                        cli._quality(ok, {"judged": True, "unsupported": ["見出し"]}))


class FocusTest(unittest.TestCase):
    def test_kind_of(self):
        def kind(source, url, **extra):
            return rank.kind_of(Candidate(source, "t", url, 1, extra=extra), blog_hosts=("engineering.dena.com",))
        self.assertEqual(kind("hackernews", "https://example.com/post"), "news")
        self.assertEqual(kind("hackernews", "https://github.com/a/b"), "oss")
        self.assertEqual(kind("hackernews", "https://github.com/a/b/issues/3"), "news")  # issue はリポジトリの紹介ではない
        self.assertEqual(kind("hatena", "https://zenn.dev/u/articles/x"), "blogs")
        self.assertEqual(kind("hatena", "https://foo.hatenablog.com/entry/1"), "blogs")
        self.assertEqual(kind("hatena", "https://engineering.dena.com/blog/2026/10/x/"), "blogs")  # config のブログ
        self.assertEqual(kind("hatena", "https://www.itmedia.co.jp/news/articles/1.html"), "news")
        self.assertEqual(kind("hackernews", "https://arxiv.org/abs/2609.01234"), "papers")
        self.assertEqual(kind("techblogs", "https://techblog.example.co.jp/a"), "blogs")
        self.assertEqual(kind("official", "https://openai.com/index/x"), "news")
        self.assertEqual(kind("github_trending", "https://github.com/a/b"), "oss")
        self.assertEqual(kind("hf_models", "https://huggingface.co/a/b"), "oss")
        self.assertEqual(kind("hf_papers", "https://arxiv.org/abs/2609.01234"), "papers")

    def _cands(self):
        return ([Candidate("hackernews", f"n{i}", f"https://news{i}.com/a", 100 - i) for i in range(4)]
                + [Candidate("zenn", f"z{i}", f"https://zenn.dev/u/articles/{i}", 50 - i) for i in range(4)]
                + [Candidate("github", f"g{i}", f"https://github.com/o/r{i}", 30 - i) for i in range(4)])

    def test_weights_filter_and_reorder(self):
        short, _ = rank.shortlist(self._cands(), [], 40, weights={"news": 0, "blogs": 1, "oss": 0, "papers": 0})
        self.assertEqual([c.title for c in short], ["z0", "z1", "z2", "z3"])
        self.assertEqual({c.extra["kind"] for c in short}, {"blogs"})
        # 重みが大きい種類は、同じ順位でも上に来る
        short, _ = rank.shortlist(self._cands(), [], 40, weights={"news": 1, "blogs": 1, "oss": 3, "papers": 1})
        self.assertEqual([c.title for c in short[:4]], ["g0", "g1", "g2", "g3"])
        # 重みを渡さなければ今までどおり（取得元ごとの1位が並ぶ）
        short, _ = rank.shortlist(self._cands(), [], 40)
        self.assertEqual({c.title for c in short[:3]}, {"n0", "z0", "g0"})

    def test_per_source_cap_grows_when_sources_are_few(self):
        many = [Candidate("hf_papers", f"p{i}", f"https://arxiv.org/abs/2609.{10000 + i}", 100 - i) for i in range(30)]
        short, _ = rank.shortlist(many + self._cands(), [], 40, weights={"news": 0, "blogs": 0, "oss": 0, "papers": 1})
        self.assertEqual(len(short), 30)  # 1つの取得元だけになるときは、取得元ごとの上限 10 件で切らない
        short, _ = rank.shortlist(many + self._cands(), [], 40)
        self.assertEqual(sum(c.source == "hf_papers" for c in short), 10)

    def test_focus_weights(self):
        self.assertEqual(rank.focus_weights({}), ("balanced", {"news": 1.0, "blogs": 1.0, "oss": 1.0, "papers": 1.0}))
        self.assertEqual(rank.focus_weights({"focus": "news"}, "blogs")[1], {"news": 0.0, "blogs": 1.0, "oss": 0.0, "papers": 0.0})
        name, w = rank.focus_weights({"focus": "custom", "weights": {"blogs": 2, "papers": 0, "oss": -1}})
        self.assertEqual(w, {"news": 1.0, "blogs": 2.0, "oss": 0.0, "papers": 0.0})
        self.assertIn("テックブログ 2、ニュース・発表 1", rank.focus_hint(name, w))
        self.assertIsNone(rank.focus_hint("balanced", rank.focus_weights({})[1]))
        self.assertIsNone(rank.focus_hint("custom", rank.focus_weights({"focus": "custom"})[1]))  # すべて同じ重み
        with self.assertRaises(ValueError):
            rank.focus_weights({"focus": "blog"})
        with self.assertRaises(ValueError):
            rank.focus_weights({"focus": "custom", "weights": {"news": 0, "blogs": 0, "oss": 0, "papers": 0}})


class SourcesTest(unittest.TestCase):
    RSS = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Blog</title><link>https://b.example/</link>
      <item><title><![CDATA[A &amp; B]]></title><link>https://b.example/a?utm_source=rss</link>
        <description>&lt;p&gt;Hello  &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description><pubDate>Wed, 30 Sep 2026 04:34:25 +0000</pubDate></item>
      <item><title>no date</title><link>https://b.example/b</link></item></channel></rss>"""
    ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title><link href="https://a.example/"/>
      <entry><title>記事</title><link rel="self" href="https://a.example/self"/><link rel="alternate" href="https://a.example/1"/>
        <published>2026-09-29T10:00:00+09:00</published><updated>2026-10-01T00:00:00Z</updated><summary>s</summary></entry></feed>""".encode()
    RDF = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"><item rdf:about="https://r.example/1"><title>R</title>
      <link>https://r.example/1</link><dc:date>2026-09-28T00:00:00Z</dc:date></item></rdf:RDF>"""

    def test_parse_feed(self):
        from daily_trend import sources
        rss = sources.parse_feed(self.RSS)
        self.assertEqual([(e["title"], e["url"], e["summary"]) for e in rss], [("A & B", "https://b.example/a?utm_source=rss", "Hello world")])
        self.assertEqual(rss[0]["published"].isoformat(), "2026-09-30T04:34:25+00:00")
        atom = sources.parse_feed(self.ATOM)
        self.assertEqual((atom[0]["title"], atom[0]["url"]), ("記事", "https://a.example/1"))
        self.assertEqual(atom[0]["published"].isoformat(), "2026-09-29T10:00:00+09:00")  # updated より published
        self.assertEqual(sources.parse_feed(self.RDF)[0]["url"], "https://r.example/1")

    def test_feed_urls_ignores_malformed_config(self):
        from daily_trend import sources
        self.assertEqual(sources.feed_urls({"techblogs": {"A": "https://a.example/feed", "B": 3, "C": "feed.xml"}}, "techblogs"),
                         {"A": "https://a.example/feed"})
        self.assertEqual(sources.feed_urls({"techblogs": "https://a.example/feed"}, "techblogs"), {})
        self.assertEqual(sources.feed_urls({}, "official"), {})

    def test_github_new_repos_are_ranked_by_stars_per_day(self):
        from datetime import datetime, timezone
        from daily_trend import sources
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        def repo(name, stars, created):
            return {"full_name": name, "html_url": f"https://github.com/{name}", "stargazers_count": stars,
                    "created_at": created, "description": "d", "language": "Go", "topics": []}
        items = [repo("a/old", 9000, "2026-09-01T00:00:00Z"), repo("b/new", 2000, "2026-09-30T00:00:00Z"),
                 repo("c/today", 500, "2026-09-30T18:00:00Z"), repo("b/new", 2000, "2026-09-30T00:00:00Z")]
        out = sources.github_new_repos(items, now, "https://github.com/search")
        self.assertEqual([c.title.split(" ")[0] for c in out], ["b/new", "c/today", "a/old"])  # 2000/日, 500/日, 300/日
        self.assertIn("1日あたり約 2000", out[0].buzz[0].metric)


class RegenerateTest(unittest.TestCase):
    """trend run --force で作り直すと、今日すでに出した記事は選ばれない。"""

    DAY = "2026-10-01"

    def setUp(self):
        from daily_trend.extract import Article
        tmp = Path(tempfile.mkdtemp())
        self.select_calls = []

        self.config = {}
        self.hints = []

        def fetch_all(cfg, feeds=None):
            return [Candidate("hackernews", t, f"https://ex.com/{t}", s) for t, s in (("a", 300), ("b", 200), ("c", 100))] + [
                Candidate("zenn", "z", "https://zenn.dev/u/articles/z", 50)], {}

        def select(short, interests, model, avoid=None, focus_hint=None):
            self.select_calls.append(([c.title for c in short], avoid))
            self.hints.append(focus_hint)
            return [{"id": 0, "reason": "r"}]

        summ = {"category": "AI/ML", "headline": "h", "one_liner": "o", "why_now": "w", "terms": [], "points": []}
        patches = [
            mock.patch.object(store, "DAYS", tmp / "days"), mock.patch.object(store, "CANDIDATES", tmp / "candidates"),
            mock.patch.object(cli, "DAYS", tmp / "days"), mock.patch.object(cli, "LOGS", tmp / "logs"),
            mock.patch.object(cli, "today_str", lambda: self.DAY), mock.patch.object(cli, "load_config", lambda: self.config),
            mock.patch.object(cli.sources, "fetch_all", fetch_all), mock.patch.object(cli.summarize, "select", select),
            mock.patch.object(cli.summarize, "summarize", lambda c, art, model: dict(summ)),
            mock.patch.object(cli.summarize, "verify", lambda s, art, model: ([], {"judged": True, "unsupported": []})),
            mock.patch.object(cli.extract, "fetch", lambda c: Article("body", c.title.upper(), "ex.com", None, "en", False, "article")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, **kw) -> int:
        args = {"force": True, "if_missing": False, "dry_run": False, **kw}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.run(**args)

    def _url(self) -> str:
        return store.load_day(self.DAY)["source"]["url"]

    def test_force_picks_a_different_article_each_time(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._url(), "https://ex.com/a")
        self.assertNotIn("replaced", store.load_day(self.DAY))  # 初回は置き換えた記事が無い
        self.assertEqual(self.select_calls[-1], (["a", "z", "b", "c"], []))
        self.assertEqual(self.hints[-1], None)  # balanced では選定の指示を変えない

        self.assertEqual(self._run(), 0)
        self.assertEqual(self._url(), "https://zenn.dev/u/articles/z")
        self.assertEqual(self.select_calls[-1], (["z", "b", "c"], ["A"]))  # 既出は候補から外し、題名を選定に渡す

        self.assertEqual(self._run(), 0)
        self.assertEqual(self._url(), "https://ex.com/b")

        self.assertEqual(self._run(), 0)
        rec = store.load_day(self.DAY)
        self.assertEqual(rec["source"]["url"], "https://ex.com/c")
        self.assertEqual([r["url"] for r in rec["replaced"]],
                         ["https://ex.com/a", "https://zenn.dev/u/articles/z", "https://ex.com/b"])
        # 置き換える前の記事も、翌日以降は既出として扱う
        self.assertEqual([h["url"] for h in store.history("2026-10-02", 30)],
                         ["https://ex.com/a", "https://zenn.dev/u/articles/z", "https://ex.com/b", "https://ex.com/c"])

        self.assertEqual(self._run(), 1)  # 出していない候補が残っていなければ失敗し、前の1件を残す
        self.assertEqual(self._url(), "https://ex.com/c")

    def test_recent_days_are_passed_as_topics_to_avoid(self):
        for day, title in (("2026-09-20", "old"), ("2026-09-25", "recent"), ("2026-09-30", "yesterday")):
            store.save_day({"date": day, "source": {"url": f"https://past.example/{title}", "title": title}}, "")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.select_calls[-1][1], ["recent", "yesterday"])  # 7日より前の話題は渡さない
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.select_calls[-1][1], ["recent", "yesterday", "A"])

    def test_without_force_keeps_the_record(self):
        self.assertEqual(self._run(), 0)
        n = len(self.select_calls)
        self.assertEqual(self._run(force=False), 0)
        self.assertEqual(self._run(force=True, if_missing=True), 0)  # launchd の定時実行は作り直さない
        self.assertEqual(len(self.select_calls), n)
        self.assertEqual(self._url(), "https://ex.com/a")

    def test_dry_run_does_not_replace(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._run(dry_run=True), 0)
        self.assertEqual(self.select_calls[-1], (["z", "b", "c"], ["A"]))
        self.assertEqual(self._url(), "https://ex.com/a")
        self.assertNotIn("replaced", store.load_day(self.DAY))


    def test_focus_limits_the_kind(self):
        self.assertEqual(self._run(focus="blogs"), 0)  # --focus blogs
        self.assertEqual(self.select_calls[-1][0], ["z"])
        self.assertIn("技術ブログ", self.hints[-1])
        rec = store.load_day(self.DAY)
        self.assertEqual((rec["focus"], rec["kind"]), ("blogs", "blogs"))
        self.assertEqual(self._run(focus="blogs"), 1)  # テックブログの未表示の候補が無いので失敗し、前の1件を残す
        self.assertEqual(self._url(), "https://zenn.dev/u/articles/z")

        self.config = {"pick": {"focus": "news"}}  # config.toml の既定
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.select_calls[-1][0], ["a", "b", "c"])
        self.assertEqual(self._run(focus="balanced"), 0)  # コマンドの指定が config より優先
        self.assertEqual(self.select_calls[-1][0], ["b", "c"])

    def test_bad_focus_in_config_falls_back_to_balanced(self):
        self.config = {"pick": {"focus": "blog"}}  # 書き間違い。毎朝の生成は止めない
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.select_calls[-1][0], ["a", "z", "b", "c"])


class SelectPromptTest(unittest.TestCase):
    def _prompt(self, avoid):
        calls = []

        def fake_call(model, system, user, schema, effort):
            calls.append((system, user))
            return {"picks": [{"id": 0, "reason": "r"}]}

        with mock.patch.object(summarize, "_call", fake_call):
            summarize.select([Candidate("hackernews", "T", "https://ex.com/t", 1)], "x", "m", avoid=avoid)
        return calls[0]

    def test_avoid_is_added_when_there_are_shown_articles(self):
        system, user = self._prompt(["Shown title"])
        self.assertIn("同じ話題", system.splitlines()[-1])
        self.assertTrue(user.endswith("# 最近すでに出した記事\n- Shown title"))
        system0, user0 = self._prompt([])
        self.assertNotIn("すでに出した記事", system0 + user0)  # 出した記事が無ければプロンプトを変えない
        self.assertTrue(system.startswith(system0) and user.startswith(user0))


class ExtractTest(unittest.TestCase):
    def test_github_routing(self):
        from daily_trend import extract
        self.assertTrue(extract.is_repo_top(extract.github_parts("https://github.com/a/b")))
        self.assertTrue(extract.is_repo_top(extract.github_parts("https://github.com/a/b/tree/main")))
        self.assertFalse(extract.is_repo_top(extract.github_parts("https://github.com/a/b/issues/3")))
        self.assertIsNone(extract.github_parts("https://github.com/orgs/community/discussions/1"))

    def test_robots_matching(self):
        from daily_trend import common
        common._ROBOTS["https://r.test"] = [(False, "/*/private"), (True, "/a/private/ok$"), (False, "/x")]
        self.assertFalse(common.robots_allowed("https://r.test/a/private/z"))
        self.assertTrue(common.robots_allowed("https://r.test/a/private/ok"))
        self.assertFalse(common.robots_allowed("https://r.test/xyz"))
        self.assertTrue(common.robots_allowed("https://r.test/public"))
        common._ROBOTS["https://down.test"] = None  # robots.txt が 5xx → 全面拒否
        self.assertFalse(common.robots_allowed("https://down.test/a"))


if __name__ == "__main__":
    unittest.main()
