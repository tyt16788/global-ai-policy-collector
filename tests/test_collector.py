# -*- coding: utf-8 -*-
"""collector 纯函数单元测试（不联网）。运行: python -m unittest discover tests -v"""

import unittest
from datetime import datetime

from collector.normalize import (normalize_url, make_guid, make_content_hash,
                                 parse_date_flexible, strip_html, guess_language, tag_ai)


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


if __name__ == "__main__":
    unittest.main()
