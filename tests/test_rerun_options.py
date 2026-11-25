"""다시 찾기 옵션: 더 싼 곳 / 단가 상한 / 새 공급처만 / 추가 조건 — 소싱 조건과 1차 필터에 반영 (API 호출 없음)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trade_hub import db

from sourcing_agent import fx, hub
from sourcing_agent.discover import prefilter


class RerunOptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        # 고정 환율 (네트워크 없이): 1 USD = 1,400원 = 7 CNY = 150 JPY
        self.fx = mock.patch.object(fx, "per_usd", return_value=({"USD": 1.0, "KRW": 1400.0, "CNY": 7.0, "JPY": 150.0},
                                                                 "테스트 환율"))
        self.fx.start()
        self.conn = db.connect()
        with self.conn:
            self.pid = db.upsert_product(self.conn, "75% 키보드", stage="sourcing")
        hub.save_results(self.conn, None, self.pid,
                         [{"name": "Old A Co., Ltd.", "country": "China", "total": 70, "grade": "B", "unit_price_usd": 36},
                          {"name": "Old B", "country": "China", "total": 60, "grade": "B", "unit_price_usd": 48}],
                         "r.md", purchase_mode="oem")

    def tearDown(self):
        self.conn.close()
        self.fx.stop()
        self.env.stop()
        self.tmp.cleanup()

    def req(self, **options):
        p = self.conn.execute("SELECT * FROM products WHERE id = ?", (self.pid,)).fetchone()
        return hub.build_request(self.conn, p, "oem", options=options)

    def test_no_options_unchanged(self):
        r = self.req()
        self.assertNotIn("max_unit_price_usd", r)
        self.assertNotIn("rerun_options", r)
        self.assertEqual(r["shortlist_size"], 8)

    def test_cheaper_and_new_only(self):
        r = self.req(cheaper=True, new_only=True, note="무브랜드 OEM 위주")
        self.assertEqual(r["max_unit_price_usd"], 36)
        self.assertEqual(r["exclude_suppliers"], ["Old A Co., Ltd.", "Old B"])
        self.assertTrue(r["notes"].startswith("## 다시 찾기 조건"))
        self.assertIn("무브랜드 OEM 위주", r["notes"])

    def test_max_price_krw_lower_wins(self):
        r = self.req(cheaper=True, max_price_krw=35000)          # 35,000원 = USD 25 < 최저 36
        self.assertEqual(r["max_unit_price_usd"], 25)
        self.assertEqual(r["max_unit_price_krw"], 35000)
        self.assertEqual(r["max_unit_price_local"]["currency"], "CNY")   # 1번 추천 국가 없음 → China
        self.assertEqual(r["max_unit_price_local"]["amount"], 175)
        self.assertIn("CNY 175.00", r["notes"])
        self.assertEqual(self.req(cheaper=True, max_price_krw=70000)["max_unit_price_usd"], 36)

    def test_local_currency_by_country(self):
        p = self.conn.execute("SELECT * FROM products WHERE id = ?", (self.pid,)).fetchone()
        jp = hub.build_request(self.conn, p, "oem", country="Japan", options={"max_price_krw": 28000})
        self.assertEqual(jp["max_unit_price_local"]["currency"], "JPY")
        self.assertEqual(jp["max_unit_price_local"]["amount"], 3000)
        us = hub.build_request(self.conn, p, "oem", country="USA", options={"max_price_krw": 28000})
        self.assertEqual(us["max_unit_price_local"], {"currency": "USD", "amount": 20, "krw_per_unit": 1400,
                                                      "fx_source": "테스트 환율"})

    def test_other_mode_not_counted(self):
        p = self.conn.execute("SELECT * FROM products WHERE id = ?", (self.pid,)).fetchone()
        r = hub.build_request(self.conn, p, "small_batch", options={"cheaper": True, "new_only": True})
        self.assertNotIn("exclude_suppliers", r)
        self.assertIn("단가가 확인된 공급처가 없어", r["notes"])

    def test_prefilter_drops_found_and_expensive(self):
        cs = [{"name": "Old A Co Ltd", "price_range": "USD 20"}, {"name": "New 1", "price_range": "USD 30-37"},
              {"name": "New 2", "price_range": "USD 25"}, {"name": "New 3"}, {"name": "New 4", "price_range": "USD 18-20"}]
        out = prefilter(cs, {"sourcing_type": "oem", "max_unit_price_usd": 26, "exclude_suppliers": ["Old A Co., Ltd."]})
        self.assertEqual([c["name"] for c in out], ["New 4", "New 2", "New 3"])


if __name__ == "__main__":
    unittest.main()
