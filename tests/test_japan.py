"""일본 공급처 발굴: 국가별 구매 사이트, 일본어 검색 링크, DB 요청의 국가."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from trade_hub import db

from sourcing_agent import hub
from sourcing_agent.channels import build_channels, load_channels, request_country, sites_for


class JapanChannelsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_channels()

    def test_sites_by_country(self):
        self.assertEqual(list(sites_for(self.cfg, "retail", "Japan")), ["rakuten", "amazon_jp", "yahoo_jp"])
        self.assertIn("superdelivery", sites_for(self.cfg, "small_batch", "Japan"))
        self.assertNotIn("rakuten", sites_for(self.cfg, "retail", "China"))
        self.assertIn("aliexpress", sites_for(self.cfg, "retail", "China"))

    def test_country_from_request(self):
        self.assertEqual(request_country({}), "China")
        self.assertEqual(request_country({"preferred_countries": ["일본"]}), "Japan")
        self.assertEqual(request_country({"preferred_countries": ["Japan", "China"]}), "Japan")

    def test_japanese_search_links(self):
        req = {"sourcing_type": "retail", "preferred_countries": ["Japan"], "product_en": "Hobonichi planner"}
        ch = build_channels(self.cfg, req, {"keyword_ja": "ほぼ日手帳"})
        self.assertEqual({c["key"] for c in ch}, {"rakuten", "amazon_jp", "yahoo_jp"})
        self.assertTrue(all("ほぼ日手帳" in unquote(c["search_url"]) for c in ch))
        # 일본어 검색어가 없으면 영문으로 대신
        ch = build_channels(self.cfg, req, None)
        self.assertTrue(all("Hobonichi" in unquote(c["search_url"]) for c in ch))


class JapanRequestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        self.conn = db.connect()
        with self.conn:
            pid = db.upsert_product(self.conn, "호보니치 테초", stage="sourcing")
        self.p = self.conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_default_china(self):
        r = hub.build_request(self.conn, self.p)
        self.assertEqual(r["preferred_countries"], ["China"])
        self.assertNotIn("일본", r.get("notes") or "")

    def test_reference_url_from_market_research(self):
        r = hub.build_request(self.conn, self.p)
        self.assertNotIn("reference_urls", r)          # 1번 참고 페이지가 없으면 넣지 않음
        url = "https://www.alibaba.com/product-detail/x_1600.html"
        with self.conn:
            self.conn.execute("UPDATE products SET ref_url = ? WHERE id = ?", (url, self.p["id"]))
        p = self.conn.execute("SELECT * FROM products WHERE id = ?", (self.p["id"],)).fetchone()
        self.assertEqual(hub.build_request(self.conn, p)["reference_urls"], [url])
        with self.conn:
            self.conn.execute("UPDATE products SET ref_url = 'images/1.jpg' WHERE id = ?", (self.p["id"],))
        p = self.conn.execute("SELECT * FROM products WHERE id = ?", (self.p["id"],)).fetchone()
        self.assertNotIn("reference_urls", hub.build_request(self.conn, p))   # http(s) 주소만

    def test_japan(self):
        r = hub.build_request(self.conn, self.p, "retail", 10, country="Japan")
        self.assertEqual(r["preferred_countries"], ["Japan"])
        self.assertIn("일본어", r["notes"])


if __name__ == "__main__":
    unittest.main()
