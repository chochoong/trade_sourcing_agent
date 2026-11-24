"""소량 사입 단가 기준: 국내 판매가에서 역산한 단가 상한 + 소매 마켓 판매자 후보 수 제한."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trade_hub import db

from sourcing_agent import hub
from sourcing_agent.discover import is_retail_listing, prefilter


class CeilingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        self.conn = db.connect()
        with self.conn:
            self.pid = db.upsert_product(self.conn, "키캡 키링", stage="sourcing", est_retail_krw=4000)
            self.conn.execute("INSERT INTO product_scores (product_id, est_fob_usd) VALUES (?, 0.6)", (self.pid,))

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def product(self, **fields):
        with self.conn:
            for k, v in fields.items():
                self.conn.execute(f"UPDATE products SET {k} = ? WHERE id = ?", (v, self.pid))
        return self.conn.execute("SELECT * FROM products WHERE id = ?", (self.pid,)).fetchone()

    def test_formula(self):
        # 4,000 ÷ 1.1 × (1 − 0.12 − 0.25) ÷ 1,400 ÷ 1.08 − 0.5 = 1.015
        usd, text = hub.price_ceiling(self.product())
        self.assertEqual(usd, 1.02)
        self.assertIn("4,000원", text)
        usd2, _ = hub.price_ceiling(self.product(duty_rate=0.0, est_shipping_usd=0.2))   # DB 값이 있으면 그 값
        self.assertGreater(usd2, usd)

    def test_small_batch_request_uses_ceiling(self):
        req = hub.build_request(self.conn, self.product(), "small_batch")
        self.assertEqual(req["price_ceiling_usd"], 1.02)
        self.assertTrue(req["target_price"].startswith("소량 구매 단가 상한 약 USD 1.02"))
        self.assertIn("참고: 대량 FOB 약 USD 0.6", req["target_price"])

    def test_oem_keeps_fob(self):
        req = hub.build_request(self.conn, self.product(), "oem")
        self.assertNotIn("price_ceiling_usd", req)
        self.assertTrue(req["target_price"].startswith("FOB 약 USD 0.6"))

    def test_too_cheap_to_sell(self):
        usd, text = hub.price_ceiling(self.product(est_retail_krw=1000))
        self.assertEqual(usd, 0.0)
        self.assertIn("남는 단가가 없음", text)

    def test_no_retail_price(self):
        self.assertIsNone(hub.price_ceiling(self.product(est_retail_krw=None)))


class RetailCapTest(unittest.TestCase):
    def cand(self, name, url, moq="1"):
        return {"name": name, "urls": [url], "moq": moq, "company_type": "trading"}

    def test_small_batch_caps_retail_sellers(self):
        ali = [self.cand(f"ali{i}", f"https://www.aliexpress.com/item/{i}.html") for i in range(5)]
        whole = [self.cand(f"w{i}", f"https://detail.1688.com/offer/{i}.html", moq="50") for i in range(2)]
        short = prefilter(ali + whole, {"sourcing_type": "small_batch", "max_order_qty": 50, "shortlist_size": 5})
        names = [c["name"] for c in short]
        self.assertEqual(len(names), 5)
        self.assertEqual(sum(n.startswith("w") for n in names), 2)        # 도매 2곳은 모두 남음
        self.assertEqual(sum(n.startswith("ali") for n in names), 3)      # 도매가 모자라서 소매로 채움 (2 + 1)
        many = [self.cand(f"w{i}", f"https://www.alibaba.com/product-detail/x_{i}.html") for i in range(6)]
        short = prefilter(ali + many, {"sourcing_type": "small_batch", "shortlist_size": 5})
        self.assertEqual(sum(c["name"].startswith("ali") for c in short), 2)   # 도매가 충분하면 소매는 2곳까지

    def test_retail_mode_not_capped(self):
        ali = [self.cand(f"ali{i}", f"https://www.aliexpress.com/item/{i}.html") for i in range(5)]
        self.assertEqual(len(prefilter(ali, {"sourcing_type": "retail", "shortlist_size": 5})), 5)

    def test_is_retail_listing(self):
        self.assertTrue(is_retail_listing({"urls": ["https://www.ebay.com/itm/123"]}))
        self.assertFalse(is_retail_listing({"urls": ["https://www.aliexpress.com/item/1.html",
                                                     "https://abc.en.alibaba.com"]}))
        self.assertFalse(is_retail_listing({"urls": []}))


if __name__ == "__main__":
    unittest.main()
