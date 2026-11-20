"""미국(eBay 등) 공급처 발굴과 1번 추천 국가(products.source_country)를 기본값으로 쓰는지."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from trade_hub import db

from sourcing_agent import hub
from sourcing_agent.channels import build_channels, load_channels, request_country, sites_for


class UsaChannelsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_channels()

    def test_sites(self):
        self.assertEqual(list(sites_for(self.cfg, "retail", "USA")), ["ebay", "amazon_us", "walmart"])
        self.assertIn("ebay", sites_for(self.cfg, "small_batch", "USA"))
        self.assertIn("thomasnet", sites_for(self.cfg, "oem", "USA"))
        self.assertNotIn("ebay", sites_for(self.cfg, "retail", "China"))

    def test_alias_and_links(self):
        self.assertEqual(request_country({"preferred_countries": ["미국"]}), "USA")
        req = {"sourcing_type": "retail", "preferred_countries": ["USA"], "product_en": "vintage film camera"}
        ch = build_channels(self.cfg, req, None)
        self.assertIn("https://www.ebay.com/sch/i.html?_nkw=vintage+film+camera",
                      [unquote(c["search_url"]).replace(" ", "+") for c in ch])


class RecommendedCountryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def product(self, name, **fields):
        with self.conn:
            pid = db.upsert_product(self.conn, name, stage="sourcing", **fields)
        return self.conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()

    def test_uses_recommended_country(self):
        p = self.product("빈티지 필름 카메라", source_country="USA")
        r = hub.build_request(self.conn, p)
        self.assertEqual(r["preferred_countries"], ["USA"])
        self.assertIn("eBay", r["notes"])

    def test_explicit_country_wins(self):
        p = self.product("빈티지 필름 카메라", source_country="USA")
        self.assertEqual(hub.build_request(self.conn, p, country="Japan")["preferred_countries"], ["Japan"])

    def test_source_page_becomes_first_candidate_note(self):
        url = "https://detail.1688.com/offer/123.html"
        p = self.product("키캡 세트", source_page_url=url)
        notes = hub.build_request(self.conn, p)["notes"]
        self.assertIn(url, notes)
        self.assertIn("첫 후보", notes)
        self.assertNotIn("해외 판매 페이지", hub.build_request(self.conn, self.product("다른 품목")).get("notes") or "")

    def test_no_recommendation_falls_back_to_china(self):
        p = self.product("키캡")
        self.assertEqual(hub.build_request(self.conn, p)["preferred_countries"], ["China"])


if __name__ == "__main__":
    unittest.main()
