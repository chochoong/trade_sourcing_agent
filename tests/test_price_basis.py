"""가격 기준 (price_basis): 확인한 키만 정리, 마켓 API 보충, DB 저장, 리포트 표시."""
import json
import unittest

from sourcing_agent.marketplaces import enrich
from sourcing_agent.price_basis import normalize_price_basis, price_basis_text
from sourcing_agent.report import _facts


class PriceBasisTest(unittest.TestCase):
    def test_normalize_keeps_known_keys_only(self):
        b = normalize_price_basis({"currency": "jpy", "low": "42,000", "high": 21000, "fx_assumed": 154,
                                   "includes_shipping": "false", "includes_tax": None, "preorder": True,
                                   "kind": "리셀", "variant": "M6a 30주년 박스",
                                   "evidence": [{"claim": "21,000円", "url": "u"}, "x"]})
        self.assertEqual(b, {"currency": "JPY", "low": 21000, "high": 42000, "fx_assumed": 154,
                             "includes_shipping": False, "preorder": True, "kind": "resale",
                             "variant": "M6a 30주년 박스", "evidence": [{"claim": "21,000円", "url": "u"}]})
        self.assertNotIn("includes_tax", b)            # 모르면 키 없음
        # includes_tax 는 DDP(한국 관부가세 포함)일 때만 — false 는 키 없음
        self.assertNotIn("includes_tax", normalize_price_basis({"kind": "retail", "includes_tax": False}))
        self.assertTrue(normalize_price_basis({"includes_tax": "true"})["includes_tax"])
        self.assertIsNone(normalize_price_basis({"currency": None, "kind": "모름", "includes_shipping": "?"}))
        self.assertIsNone(normalize_price_basis(None))
        self.assertNotIn("fx_assumed", normalize_price_basis({"currency": "USD", "low": 1, "fx_assumed": 1}))

    def test_market_api_fills_missing(self):
        snap = {"source": "AliExpress", "listings": [
            {"url": "https://www.aliexpress.com/item/1005001234567.html", "price_usd": 1.25}]}
        r = enrich({"urls": ["https://www.aliexpress.com/item/1005001234567.html"],
                    "price_basis": {"includes_shipping": True}}, snap)
        b = r["price_basis"]
        self.assertEqual((b["currency"], b["low"], b["kind"], b["includes_shipping"]), ("USD", 1.25, "retail", True))

    def test_verify_fills_variant_and_retail_kind(self):
        from sourcing_agent.scoring import load_rubric
        from sourcing_agent.verify import verify_one

        class FakeLLM:
            def ask_json(self, *a, **k):
                return {"scores": {}, "price_basis": {"includes_shipping": "true"}}
        r = verify_one(FakeLLM(), {"name": "x", "urls": ["https://www.aliexpress.com/item/1005001234567.html"],
                                   "variant": "2구 기본형"}, {"product": "p"}, load_rubric("small_batch"))
        self.assertEqual(r["price_basis"], {"includes_shipping": True, "variant": "2구 기본형", "kind": "retail"})

    def test_text(self):
        self.assertEqual(price_basis_text({"currency": "JPY", "low": 21000, "high": 42000, "includes_shipping": False,
                                           "kind": "resale", "preorder": True, "variant": "M5 박스"}),
                         "JPY 21,000~42,000 · 배송비 별도 · 리셀 · 예약판매 — M5 박스")
        rows = dict(_facts({"price_basis": {"kind": "wholesale"}}))
        self.assertEqual(rows["가격 기준"], "도매")

    def test_hub_saves(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock

        from trade_hub import db

        from sourcing_agent import hub
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,                 mock.patch.dict(os.environ, {"TRADE_DB": str(Path(tmp) / "t.db")}):
            conn = db.connect()
            with conn:
                pid = db.upsert_product(conn, "키캡 키링", stage="sourcing")
                run = conn.execute("INSERT INTO agent_runs (agent, status) VALUES ('sourcing', 'succeeded')").lastrowid
            r = {"name": "Shop A", "country": "China", "price_range": "USD 0.53-0.79", "total": 60, "grade": "B",
                 "trade_terms": {"lead_time": "7d"},
                 "price_basis": {"currency": "USD", "low": 0.53, "high": 0.79, "kind": "wholesale",
                                 "variant": "4구 LED형"}}
            hub.save_results(conn, run, pid, [r], "x", purchase_mode="small_batch", max_order_qty=100)
            row = conn.execute("SELECT price_note, trade_terms_json, max_order_qty, unit_price_usd"
                               " FROM product_suppliers").fetchone()
            self.assertEqual(row[0], "USD 0.53-0.79 — 4구 LED형")
            terms = json.loads(row[1])
            self.assertEqual((terms["price_basis"]["kind"], terms["lead_time"]), ("wholesale", "7d"))
            self.assertEqual((row[2], row[3]), (100, 0.79))
            # 다시 조사했는데 가격 기준을 모르면 예전 값을 이어받지 않음 (가격은 바뀜)
            hub.save_results(conn, run, pid, [{**r, "price_basis": None}], "x", purchase_mode="small_batch")
            terms = json.loads(conn.execute("SELECT trade_terms_json FROM product_suppliers").fetchone()[0])
            self.assertNotIn("price_basis", terms)
            conn.close()

if __name__ == "__main__":
    unittest.main()
