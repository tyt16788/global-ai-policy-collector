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


if __name__ == "__main__":
    unittest.main()
