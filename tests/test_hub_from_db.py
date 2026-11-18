"""from-db: 공급처·구매 방식·구매 사이트 순위를 공용 DB에 저장 (API 호출 없이 가짜 조사 결과로)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trade_hub import db

from sourcing_agent import hub

CHANNELS = [{"rank": 1, "key": "aliexpress", "site": "AliExpress", "search_url": "https://a/x", "total": 84,
             "scores": {"small_qty": 20}, "typical_price": "USD 12-18", "min_qty": "1", "summary": "낱개 가능",
             "site_note": "", "evidence": [], "assessed": True},
            {"rank": 2, "key": "1688", "site": "1688", "search_url": "https://b/x", "total": 58,
             "scores": {}, "typical_price": None, "min_qty": None, "summary": None, "site_note": "",
             "evidence": [], "assessed": False}]


class FromDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # from-db 연결이 열려 있어도 정리
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        conn = db.connect()
        with conn:
            self.pid = db.upsert_product(conn, "AI안경", stage="sourcing")
        conn.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_verify(self, stype):
        run_dir = Path(self.tmp.name) / "run"
        run_dir.mkdir(exist_ok=True)
        payload = {"sourcing_type": stype, "request": {"max_order_qty": 10, "trade_hub_product_id": self.pid},
                   "suppliers": [{"name": "A", "country": "China", "moq": "1 piece", "total": 70, "grade": "B",
                                  "urls": ["https://www.aliexpress.com/item/1.html"], "unit_price_usd": 20}],
                   "channels": CHANNELS}
        (run_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"run_dir": str(run_dir), "files": {"json": str(run_dir / "results.json"),
                                                   "markdown": str(run_dir / "report.md")}, "top3": []}

    def run_hub(self, stype):
        with mock.patch.object(hub, "run_discover", return_value={"run_dir": "x"}), \
             mock.patch.object(hub, "run_verify", side_effect=lambda *a, **k: self.fake_verify(stype)), \
             mock.patch.object(hub, "LLM") as llm, \
             mock.patch.object(hub, "export_to_buyagent", return_value={"summary": "ok"}) as exp:
            llm.return_value.model = "fake"
            out = hub.run_from_db([self.pid], stype, log=lambda m: None)
        return out, exp

    def test_saves_suppliers_mode_and_channels(self):
        out, exp = self.run_hub("retail")
        conn = db.connect()
        link = conn.execute("SELECT purchase_mode, max_order_qty FROM product_suppliers").fetchone()
        self.assertEqual((link["purchase_mode"], link["max_order_qty"]), ("retail", 10))
        ch = db.latest_channels(conn, self.pid, "retail")
        self.assertEqual([c["site"] for c in ch], ["AliExpress", "1688"])
        self.assertEqual(ch[0]["typical_price"], "USD 12-18")     # 상위 3 요약본이 아니라 전체 channels
        self.assertEqual(ch[1]["assessed"], 0)
        conn.close()
        exp.assert_called_once()                                  # retail 이면 buyagent 로 내보냄
        self.assertEqual(out["processed"][0]["buyagent"], {"summary": "ok"})

    def test_small_batch_not_exported(self):
        _, exp = self.run_hub("small_batch")
        exp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
