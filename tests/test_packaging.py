"""포장·무게: 단위 통일, 개당 무게·부피 계산, 모르면 None, 공용 DB trade_terms_json.packaging 저장."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trade_hub import db

from sourcing_agent import hub
from sourcing_agent.packaging import normalize_packaging, packaging_text, size_cm, weight_kg


class NormalizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(weight_kg("350g"), 0.35)
        self.assertEqual(weight_kg("Gross weight: 0.35 kg"), 0.35)
        self.assertEqual(weight_kg(18.5), 18.5)
        self.assertAlmostEqual(weight_kg("2 lbs"), 0.9072, places=3)
        self.assertIsNone(weight_kg("미확인"))
        self.assertEqual(size_cm("60x40x35 cm"), [60, 40, 35])
        self.assertEqual(size_cm("300*100*50mm"), [30, 10, 5])
        self.assertEqual(size_cm("12 x 4 x 2 inches"), [30.48, 10.16, 5.08])
        self.assertEqual(size_cm([60, 40, 35]), [60, 40, 35])
        self.assertIsNone(size_cm("60x40"))

    def test_per_unit_from_carton(self):
        p = normalize_packaging({"unit_weight_kg": "320g", "pcs_per_carton": "50 pcs", "carton_weight_kg": "18.5kg",
                                 "carton_size_cm": "60*40*35cm", "evidence": ["50pcs/carton"]})
        self.assertEqual(p["per_unit_kg"], 0.37)                 # 카톤 기준 (포장재 포함) 우선
        self.assertEqual(p["per_unit_cbm"], 0.00168)
        self.assertEqual(p["evidence"], [{"claim": "50pcs/carton", "url": None}])
        self.assertIn("카톤 50개", packaging_text(p))

    def test_per_unit_from_single_pack(self):
        p = normalize_packaging({"unit_weight_kg": 0.2, "unit_size_cm": [20, 10, 5]})
        self.assertEqual((p["per_unit_kg"], p["per_unit_cbm"]), (0.2, 0.001))
        self.assertEqual(packaging_text(p), "개당 200g / 포장 20×10×5cm")

    def test_unknown_is_none(self):
        self.assertIsNone(normalize_packaging(None))
        self.assertIsNone(normalize_packaging({"unit_weight_kg": None, "carton_size_cm": "문의 필요"}))


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        self.conn = db.connect()
        with self.conn:
            self.pid = db.upsert_product(self.conn, "벽선반", stage="sourcing")

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def save(self, packaging, terms=None):
        run = self.conn.execute("INSERT INTO agent_runs (agent, status) VALUES ('sourcing', 'succeeded')").lastrowid
        r = {"name": "A", "country": "China", "total": 70, "grade": "B", "packaging": packaging,
             "trade_terms": terms or {"lead_time": "15일", "payment_terms": None, "sample": None, "export_markets": []}}
        hub.save_results(self.conn, run, self.pid, [r], "r.html", purchase_mode="small_batch")
        raw = self.conn.execute("SELECT trade_terms_json FROM product_suppliers").fetchone()[0]
        return json.loads(raw)

    def test_saved_and_kept(self):
        pack = normalize_packaging({"unit_weight_kg": 0.35})
        terms = self.save(pack)
        self.assertEqual(terms["packaging"]["per_unit_kg"], 0.35)  # 4번이 읽는 곳
        self.assertEqual(terms["lead_time"], "15일")
        terms = self.save(None)                                     # 재조사에서 못 찾으면 예전 값 유지
        self.assertEqual(terms["packaging"]["per_unit_kg"], 0.35)

    def test_no_packaging_no_key(self):
        self.assertNotIn("packaging", self.save(None))


if __name__ == "__main__":
    unittest.main()
