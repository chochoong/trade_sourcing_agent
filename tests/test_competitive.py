"""경쟁 제품 대비 평가: 값 정리, evidence_json 항목, 검증 결과·리포트 연결."""
import unittest

from sourcing_agent.competitive import competitive_evidence, competitive_text, normalize_competitive
from sourcing_agent.report import _facts

RAW = {"competitors": [{"name": "A사 G304", "url": "https://a/1", "price": "29,000원"}, {"name": ""}],
       "attributes": [{"name": "폴링레이트", "ours": "1000Hz", "competitor": "8000Hz (A사)", "verdict": "worse"},
                      {"name": "핫스왑", "ours": "지원", "competitor": "미지원", "verdict": "우위"},
                      {"name": "배터리", "ours": None, "competitor": None, "verdict": "동등"}],
       "review": {"rating": "4.6", "count": "1,200", "complaints": ["더블클릭", None]},
       "summary": "핫스왑은 우위, 폴링레이트는 열위"}


class CompetitiveTest(unittest.TestCase):
    def test_normalize(self):
        c = normalize_competitive(RAW)
        self.assertEqual([x["name"] for x in c["competitors"]], ["A사 G304"])
        self.assertEqual([(a["name"], a.get("verdict")) for a in c["attributes"]], [("폴링레이트", "열위"), ("핫스왑", "우위")])
        self.assertEqual(c["review"], {"rating": 4.6, "count": 1200, "complaints": ["더블클릭"]})
        self.assertIsNone(normalize_competitive({"attributes": [], "review": {}, "summary": "null"}))

    def test_evidence_keeps_claim_url_format(self):
        ev = competitive_evidence(normalize_competitive(RAW), "https://shop/item")
        self.assertTrue(all(set(e) == {"claim", "url", "type"} and e["type"] == "competitive" for e in ev))
        self.assertIn("경쟁 비교 [열위] 폴링레이트: 이 제품 1000Hz vs 경쟁 8000Hz (A사)", [e["claim"] for e in ev])
        self.assertTrue(ev[-1]["claim"].startswith("경쟁 대비 요약 (우위 1 · 열위 1)"))
        self.assertEqual(competitive_evidence(None), [])

    def test_report_and_verify(self):
        c = normalize_competitive(RAW)
        self.assertEqual(competitive_text(c), "우위 1 · 열위 1 — 핫스왑은 우위, 폴링레이트는 열위 — 리뷰 4.6 (불만: 더블클릭)")
        self.assertIn("경쟁 대비", dict(_facts({"competitive": c})))

        from sourcing_agent.scoring import load_rubric
        from sourcing_agent.verify import verify_one

        class FakeLLM:
            def ask_json(self, *a, **k):
                return {"scores": {}, "evidence": [{"claim": "2015년 설립", "url": "u"}], "competitive": RAW}
        r = verify_one(FakeLLM(), {"name": "x", "urls": ["https://www.aliexpress.com/item/1005001234567.html"]},
                       {"product": "p"}, load_rubric("small_batch"))
        self.assertEqual(r["evidence"][0], {"claim": "2015년 설립", "url": "u"})
        self.assertEqual(sum(e.get("type") == "competitive" for e in r["evidence"]), 5)
        self.assertEqual(r["competitive"]["summary"], "핫스왑은 우위, 폴링레이트는 열위")


if __name__ == "__main__":
    unittest.main()
