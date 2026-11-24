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


SPECS = {"flags": {"power_source": "battery_lithium", "wireless": True, "food_contact": None},
         "materials": ["TR90"], "evidence": [{"claim": "리튬 배터리", "url": "https://www.aliexpress.com/item/1.html"}]}


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
                                  "urls": ["https://www.aliexpress.com/item/1.html"], "unit_price_usd": 20,
                                  "specs": SPECS}],
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
        specs = conn.execute("SELECT specs_json FROM product_suppliers").fetchone()["specs_json"]
        self.assertEqual(json.loads(specs), SPECS)                # 3번 통관·규제 체크가 읽을 제품 사양
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

    def test_skips_mixed_variant_products(self):
        """1번이 나눈 원래 품목('[변형 혼합]')은 판매가가 섞여 있어 조사하지 않고 나눈 품목을 안내."""
        conn = db.connect()
        with conn:
            conn.execute("UPDATE products SET notes = ? WHERE id = ?",
                         ("[변형 혼합] [1번 시세] 나눔 → #66 낱개 4,000원, #67 LED형 8,000원", self.pid))
        msgs = []
        with mock.patch.object(hub, "log_stderr", msgs.append):
            self.assertEqual(hub.pending_products(conn, [self.pid]), [])
            self.assertEqual(hub.pending_products(conn), [])
        self.assertIn("#66, #67", msgs[0])
        conn.close()

    def test_rerun_replaces_top_picks(self):
        """같은 구매 방식으로 다시 조사하면 추천 = 최신 실행의 상위 3곳만. 다른 구매 방식은 그대로."""
        def sup(*names):
            return [{"name": n, "country": "China", "total": 80 - i, "grade": "B"} for i, n in enumerate(names)]

        def save(names, mode):
            run = conn.execute("INSERT INTO agent_runs (agent, status) VALUES ('sourcing', 'succeeded')").lastrowid
            hub.save_results(conn, run, self.pid, sup(*names), "r.html", purchase_mode=mode)

        def picks(mode):
            return sorted(r[0] for r in conn.execute(
                "SELECT s.company FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id"
                " WHERE ps.product_id = ? AND ps.is_top_pick = 1 AND ps.purchase_mode = ?", (self.pid, mode)))

        conn = db.connect()
        save(["A", "B", "C", "D"], "small_batch")
        save(["X", "Y"], "oem")
        save(["B", "E", "F"], "small_batch")
        self.assertEqual(picks("small_batch"), ["B", "E", "F"])   # 예전 A·C 추천 해제, B 는 이번에도 추천
        self.assertEqual(picks("oem"), ["X", "Y"])                # 다른 구매 방식은 건드리지 않음
        save([], "small_batch")                                   # 못 찾은 실행은 예전 추천 유지
        self.assertEqual(picks("small_batch"), ["B", "E", "F"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
