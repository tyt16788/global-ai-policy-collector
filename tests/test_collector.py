# -*- coding: utf-8 -*-
"""collector 单元测试（不联网）。运行: python -m unittest discover tests -v"""

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from collector.normalize import (normalize_url, make_guid, make_content_hash,
                                 parse_date_flexible, strip_html, guess_language, tag_ai)
from collector import store
from collector.sources import company_blog


class TestNormalizeUrl(unittest.TestCase):
    def test_strips_tracking_params(self):
        self.assertEqual(
            normalize_url("https://example.com/a?utm_source=x&id=1"),
            "https://example.com/a?id=1",
        )

    def test_sorts_query_params(self):
        self.assertEqual(
            normalize_url("https://example.com/a?b=2&a=1"),
            normalize_url("https://example.com/a?a=1&b=2"),
        )

    def test_http_upgraded_and_trailing_slash_removed(self):
        self.assertEqual(normalize_url("http://Example.com/a/"), "https://example.com/a")

    def test_fragment_dropped(self):
        self.assertEqual(normalize_url("https://example.com/a#sec1"), "https://example.com/a")

    def test_empty(self):
        self.assertEqual(normalize_url(""), "")


class TestGuid(unittest.TestCase):
    def test_same_url_same_guid(self):
        self.assertEqual(make_guid("https://a.com/x"), make_guid("https://a.com/x"))

    def test_url_variants_dedup_to_same_guid(self):
        self.assertEqual(
            make_guid("https://a.com/x?utm_source=rss"),
            make_guid("http://a.com/x/"),
        )

    def test_fallback_uses_title(self):
        self.assertNotEqual(make_guid("", "t1", "2024-01-01"), make_guid("", "t2", "2024-01-01"))


class TestParseDate(unittest.TestCase):
    def test_rfc2822(self):
        self.assertEqual(parse_date_flexible("Thu, 26 Oct 2026 10:00:00 +0800"),
                         datetime(2026, 10, 26, 2, 0, 0))

    def test_iso(self):
        self.assertEqual(parse_date_flexible("2026-10-16"), datetime(2026, 10, 16))

    def test_cn_style(self):
        self.assertEqual(parse_date_flexible("2026年10月16日"), datetime(2026, 10, 16))

    def test_govcn_style(self):
        self.assertEqual(parse_date_flexible("2026.10.16"), datetime(2026, 10, 16))

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_date_flexible("not a date"))


class TestText(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_guess_language(self):
        self.assertEqual(guess_language("国务院关于印发通知"), "zh")
        self.assertEqual(guess_language("Executive Order on AI"), "en")

    def test_tag_ai_zh(self):
        tags = tag_ai("国务院关于人工智能发展的规划", {"zh": ["人工智能"], "en": []})
        self.assertIn("人工智能", tags)

    def test_tag_ai_en_word_boundary(self):
        # "said" 不应命中 "AI"
        self.assertEqual(tag_ai("He said nothing", {"zh": [], "en": ["AI"]}), "")
        self.assertIn("AI", tag_ai("Policy on AI governance", {"zh": [], "en": ["AI"]}))


class TestContentHash(unittest.TestCase):
    def test_same_content_same_hash(self):
        self.assertEqual(make_content_hash("CN", "规划", "2024-01-01"),
                         make_content_hash("CN", "规划", "2024-01-01"))
        self.assertNotEqual(make_content_hash("CN", "规划", "2024-01-01"),
                            make_content_hash("US", "规划", "2024-01-01"))


class TestStore(unittest.TestCase):
    """CSV 存储层：临时目录内验证去重追加、全文回填、落盘往返。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 把存储路径指到临时目录
        patcher1 = mock.patch.object(store, "master_path", return_value=self.tmp / "policy_master.csv")
        patcher2 = mock.patch.object(store, "health_path", return_value=self.tmp / "source_health.csv")
        patcher3 = mock.patch.object(store, "runs_path", return_value=self.tmp / "runs.csv")
        for p in (patcher1, patcher2, patcher3):
            p.start()
            self.addCleanup(p.stop)
        # 重置单例
        self.addCleanup(setattr, store, "_master", None)
        self.addCleanup(setattr, store, "_guid_index", set())
        self.kw = {"zh": ["人工智能"], "en": ["AI"]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, url="https://www.gov.cn/a.htm", title="测试政策"):
        return {"title": title, "url": url, "summary": "涉及人工智能",
                "jurisdiction": "CN", "jurisdiction_name": "中国", "level": "中央",
                "issuing_body": "国务院", "policy_type": "政策文件", "language": "zh",
                "published_date": "2024-06-01", "source_name": "test", "collection_method": "test"}

    def test_insert_and_dedup(self):
        self.assertEqual(store.insert_policy(self._rec(), self.kw), "new")
        self.assertEqual(store.insert_policy(self._rec(), self.kw), "duplicate")
        df = store.load_master()
        self.assertEqual(len(df), 1)
        self.assertIn("人工智能", df.iloc[0]["ai_tags"])

    def test_fulltext_roundtrip(self):
        store.insert_policy(self._rec(), self.kw)
        guid = store.load_master().iloc[0]["guid"]
        self.assertTrue(store.set_fulltext(guid, "全文内容" * 100, "ok"))
        df = store.load_master()
        self.assertEqual(df.iloc[0]["fulltext_status"], "ok")
        self.assertGreater(len(df.iloc[0]["full_text"]), 100)

    def test_missing_fulltext_listing(self):
        store.insert_policy(self._rec(), self.kw)
        pending = store.records_missing_fulltext()
        self.assertEqual(len(pending), 1)
        store.set_fulltext(pending.iloc[0]["guid"], "内容", "ok")
        self.assertEqual(len(store.records_missing_fulltext()), 0)

    def test_persist_and_reload(self):
        store.insert_policy(self._rec(), self.kw)
        store.save()
        # 模拟重启：重置单例后重新加载
        store._master = None
        store._guid_index = set()
        self.assertEqual(len(store.load_master(force=True)), 1)

    def test_stats_and_duplicates(self):
        store.insert_policy(self._rec(), self.kw)
        s = store.stats()
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["by_jurisdiction"].get("CN"), 1)
        self.assertEqual(store.suspected_duplicates(), [])


class TestCompanyBlogClassify(unittest.TestCase):
    """信号层的分层/打标规则：治理主张 vs 技术产品。"""

    def test_governance_text(self):
        from collector.sources.company_blog import classify
        layer, tags = classify("Our policy position on AI regulation and compliance")
        self.assertEqual(layer, "governance")
        self.assertIn("政策法规", tags)

    def test_tech_text(self):
        from collector.sources.company_blog import classify
        layer, tags = classify("Introducing GPT-5 with a larger context window and a new API")
        self.assertEqual(layer, "tech")
        self.assertIn("模型能力", tags)

    def test_empty_text_falls_back_to_tech(self):
        from collector.sources.company_blog import classify
        layer, tags = classify("")
        self.assertEqual(layer, "tech")
        self.assertEqual(tags, "其他")


class TestArticleExtraction(unittest.TestCase):
    def test_article_selector_drops_chrome(self):
        from collector.sources.company_blog import extract_article
        html = ("<html><body><nav>导航菜单</nav><article>" + "正文内容" * 80 +
                "</article><footer>页脚</footer></body></html>")
        text = extract_article(html, "article")
        self.assertIn("正文内容", text)
        self.assertNotIn("导航菜单", text)
        self.assertNotIn("页脚", text)

    def test_fallback_picks_longest_container(self):
        from collector.sources.company_blog import extract_article
        html = ("<html><body><div>短文本</div><div>" + "长正文" * 300 + "</div></body></html>")
        self.assertGreater(len(extract_article(html, "article")), 200)


class TestSignalStore(unittest.TestCase):
    """信号表独立存储：与政策表同构 + 4 列信号属性，去重与全文状态独立。"""

    def setUp(self):
        from collector import signal_store
        self.ss = signal_store
        self.tmp = Path(tempfile.mkdtemp())
        for name, filename in (("signal_path", "signal_master.csv"),
                               ("health_path", "signal_health.csv")):
            p = mock.patch.object(signal_store, name, return_value=self.tmp / filename)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, signal_store, "_master", None)
        self.addCleanup(setattr, signal_store, "_guid_index", set())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, url="https://openai.com/index/x", title="A new model"):
        return {"title": title, "url": url, "summary": "s", "jurisdiction": "GLOBAL",
                "jurisdiction_name": "全球（企业信号）", "level": "企业", "issuing_body": "OpenAI",
                "policy_type": "企业博客", "language": "en", "published_date": "2026-10-01",
                "source_name": "OpenAI-Blog", "collection_method": "company_rss",
                "source_type": "corporate_blog", "signal_layer": "tech",
                "company": "OpenAI", "topic_tags": "产品发布"}

    def test_insert_keeps_signal_columns_and_dedups(self):
        self.assertEqual(self.ss.insert(self._rec()), "new")
        self.assertEqual(self.ss.insert(self._rec()), "duplicate")
        df = self.ss.load()
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["company"], "OpenAI")
        self.assertEqual(df.iloc[0]["signal_layer"], "tech")
        self.assertEqual(df.iloc[0]["source_type"], "corporate_blog")

    def test_fulltext_cycle(self):
        self.ss.insert(self._rec())
        pending = self.ss.missing_fulltext()
        self.assertEqual(len(pending), 1)
        self.assertTrue(self.ss.set_fulltext(pending.iloc[0]["guid"], "全文" * 200, "ok"))
        self.assertEqual(len(self.ss.missing_fulltext()), 0)
        self.assertEqual(self.ss.stats()["fulltext_ok"], 1)


class TestCleanArticleText(unittest.TestCase):
    def test_drops_boilerplate_and_keeps_body(self):
        raw = "\n".join(["Title", "Loading…", "Share", "", "Body line one.", "Body line two."])
        out = company_blog.clean_article_text(raw)
        self.assertIn("Body line one.", out)
        self.assertNotIn("Loading", out)
        self.assertNotIn("Share", out)
        self.assertEqual(out.splitlines()[0], "Title")

    def test_collapses_spaced_out_heading(self):
        out = company_blog.clean_article_text("C h a t G P T I m a g e s 2 . 5\n正文")
        self.assertIn("ChatGPTImages2.5", out.replace(" ", ""))

    def test_trims_related_posts_block(self):
        body = [f"正文第 {i} 行。" for i in range(20)]
        tail = ["Product Oct 7, 2026 GPT-6 and Intelligent UI",
                "Company Sep 30, 2026 OpenAI extends cyber access",
                "Product Oct 2, 2026 Something else"]
        out = company_blog.clean_article_text("\n".join(body + tail))
        self.assertIn("正文第 19 行。", out)
        self.assertNotIn("Something else", out)

    def test_idempotent(self):
        raw = "A b c d e f\n正文\n\n正文"
        once = company_blog.clean_article_text(raw)
        self.assertEqual(once, company_blog.clean_article_text(once))


class TestParseHumanDate(unittest.TestCase):
    """企业博客用人类可读日期（Oct 14, 2026），此前一律解析成空。"""

    def test_month_day_year_variants(self):
        for s in ("Oct 14, 2026", "October 14, 2026", "Oct. 14, 2026",
                  "Oct 14 2026", "14 Oct 2026", "14 October 2026"):
            with self.subTest(raw=s):
                self.assertEqual(parse_date_flexible(s).strftime("%Y-%m-%d"), "2026-10-14")

    def test_rfc822_still_works(self):
        self.assertEqual(
            parse_date_flexible("Tue, 14 Oct 2026 08:30:00 GMT").strftime("%Y-%m-%d"),
            "2026-10-14",
        )


class TestOecdParse(unittest.TestCase):
    """OECD.AI 新端点（api.oecd.ai）的字段映射。"""

    def test_parse_item_maps_fields(self):
        from collector.sources import oecd
        item = {
            "id": 12345,
            "englishName": "National AI Strategy",
            "originalName": "国家人工智能战略",
            "slug": "national-ai-strategy",
            "description": "<p>Strategy text.</p>",
            "website": "https://example.gov/ai-strategy",
            "startYear": 2021,
            "category": "Legally binding",
            "extentBinding": "Binding",
            "gaiinCountry": {"name": "Germany", "code": "DEU"},
            "initiativeType": {"name": "Strategy"},
            "tags": [{"name": "governance"}],
        }
        rec = oecd._parse_item(item)
        self.assertEqual(rec["title"], "National AI Strategy")
        self.assertEqual(rec["url"], "https://example.gov/ai-strategy")
        self.assertEqual(rec["jurisdiction"], "DE")
        self.assertEqual(rec["jurisdiction_name"], "德国")
        self.assertEqual(rec["published_date"], "2021-01-01")   # 只有年份
        self.assertTrue(rec["extra"]["year_only"])
        self.assertEqual(rec["extra"]["country_code3"], "DEU")
        self.assertEqual(rec["summary"], "Strategy text.")

    def test_parse_item_without_website_falls_back_to_dashboard(self):
        from collector.sources import oecd
        rec = oecd._parse_item({"englishName": "X", "slug": "x-slug", "startYear": 2024})
        self.assertEqual(rec["url"],
                         "https://oecd.ai/en/dashboards/policy-initiatives/x-slug")

    def test_find_list_shapes(self):
        from collector.sources import oecd
        self.assertEqual(len(oecd._find_initiative_list({"data": [1, 2]})), 2)
        self.assertEqual(oecd._find_initiative_list([1, 2, 3]), [1, 2, 3])
        self.assertEqual(oecd._find_initiative_list({"foo": 1}), [])


class TestGovUkParse(unittest.TestCase):
    """gov.uk 搜索结果 Atom 的标题前缀与字段映射。"""

    def test_split_title_extracts_document_type(self):
        from collector.sources import govuk
        self.assertEqual(govuk._split_title("Policy paper: AI Regulation Bill"),
                         ("政策文件", "AI Regulation Bill"))
        self.assertEqual(govuk._split_title("Guidance: Using AI safely"),
                         ("指南", "Using AI safely"))
        # 冒号但不是已知类型 → 整串保留为标题
        self.assertEqual(govuk._split_title("AI: what it means"),
                         ("", "AI: what it means"))

    def test_parse_entry(self):
        from collector.sources import govuk
        org = "department-for-science-innovation-and-technology"
        rec = govuk._parse({
            "title": "Policy paper: National AI Strategy",
            "link": "https://www.gov.uk/government/publications/national-ai-strategy",
            "updated": "2026-10-13T15:00:38Z",
            "summary": "<p>Strategy.</p>",
        }, org)
        self.assertEqual(rec["title"], "National AI Strategy")
        self.assertEqual(rec["policy_type"], "政策文件")
        self.assertEqual(rec["published_date"], "2026-10-13")
        self.assertEqual(rec["jurisdiction"], "UK")
        self.assertIn("DSIT", rec["issuing_body"])
        self.assertEqual(rec["summary"], "Strategy.")

    def test_parse_entry_skips_incomplete(self):
        from collector.sources import govuk
        self.assertEqual(govuk._parse({"title": "只有标题"}, "x"), {})
        self.assertEqual(govuk._parse({"link": "https://x"}, "x"), {})


if __name__ == "__main__":
    unittest.main()
