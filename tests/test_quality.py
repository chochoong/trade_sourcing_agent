"""제품 품질·리뷰 평가: 값 정리, 모델별 1회 조사, evidence 항목, 리포트."""
import unittest

from sourcing_agent import quality
from sourcing_agent.quality import assess_quality, model_key, normalize_quality, quality_text
from sourcing_agent.report import _facts

RAW = {"model": "AULA F75", "review_sources": "6", "review_count_total": "3,400",
       "pros": [{"point": "타건감 좋음", "url": "https://r/1"}, "가성비"],
       "cons": [{"point": "2.4G 연결 끊김", "url": "https://r/2"}, {"point": None}],
       "spec_doubts": [{"spec": "배터리", "claimed": "4000mAh", "observed": "3~4일", "status": "exaggerated",
                        "url": "https://r/3"}, {"spec": "폴링", "status": "이상한값"}],
       "attributes": [{"name": "연결", "ours": "3모드", "competitor": "2모드", "verdict": "better"}],
       "vs_competitors": "연결은 우위, 배터리는 열위", "verdict": "GOOD", "evidence": [{"claim": "다나와", "url": "x"}]}


class FakeLLM:
    def __init__(self):
        self.calls = []

    def ask_json(self, system, user, **k):
        self.calls.append(k.get("stage"))
        return RAW


class QualityTest(unittest.TestCase):
    def test_normalize(self):
        q = normalize_quality(RAW)
        self.assertEqual((q["model"], q["review_sources"], q["review_count_total"], q["verdict"]),
                         ("AULA F75", 6, 3400, "good"))
        self.assertEqual(q["pros"][1], {"point": "가성비", "url": None})
        self.assertEqual(len(q["cons"]), 1)
        self.assertEqual([d["status"] for d in q["spec_doubts"]], ["exaggerated", "unverified"])
        self.assertEqual(q["attributes"][0]["verdict"], "우위")
        self.assertEqual(q["evidence"], [{"claim": "다나와", "url": None}])   # url 아니면 버림
        self.assertFalse(q["sample_needed"])

    def test_unknown_when_no_reviews(self):
        q = normalize_quality({"model": "X1", "verdict": "good"})
        self.assertEqual((q["verdict"], q["sample_needed"]), ("unknown", True))
        self.assertIn("샘플 확인 필요", q["note"])

    def test_model_key(self):
        self.assertEqual(model_key("AULA F75"), model_key("aula-f75 "))
        self.assertIsNone(model_key("OEM"))
        self.assertIsNone(model_key(None))

    def test_one_call_per_model(self):
        rs = [{"name": "A", "model": "AULA F75", "total": 70}, {"name": "B", "model": "aula f75", "total": 60},
              {"name": "C", "model": None, "total": 50}, {"name": "D", "model": "CIDOO QK75", "total": 40}]
        llm = FakeLLM()
        n = assess_quality(llm, rs, {"product": "키보드"}, log=lambda m: None, max_models=1)
        self.assertEqual((n, llm.calls), (1, ["quality"]))
        self.assertIs(rs[0]["quality"], rs[1]["quality"])
        self.assertEqual(rs[2]["quality"]["verdict"], "unknown")                   # 모델명 없음
        self.assertIn("초과", rs[3]["quality"]["note"])                            # 조사 대상 수 초과
        self.assertTrue(any(e.get("type") == "quality" and "연결 끊김" in e["claim"] for e in rs[0]["evidence"]))

    def test_failure_is_unknown(self):
        class Boom:
            def ask_json(self, *a, **k):
                raise ValueError("bad json")
        rs = [{"name": "A", "model": "AULA F75"}]
        assess_quality(Boom(), rs, {}, log=lambda m: None)
        self.assertEqual(rs[0]["quality"]["verdict"], "unknown")

    def test_report(self):
        q = normalize_quality(RAW)
        self.assertEqual(quality_text(q), "좋음 · 리뷰 6곳 3,400건 · 불만: 2.4G 연결 끊김 · 과장 의심: 배터리"
                                          " — 연결은 우위, 배터리는 열위")
        self.assertIn("제품 품질", dict(_facts({"quality": q})))


class GroupingSkipTest(unittest.TestCase):
    def test_same_model_variants(self):
        from sourcing_agent.quality import same_model
        a = "Pokémon Card Game 30th CELEBRATION FUTURISTIC BOX (SKU tcg0572)"
        b = "Pokemon Card Game MEGA 30th CELEBRATION FUTURISTIC BOX (YOSHIROTTEN Edition, JAN 4521329000000)"
        c = "Pokemon Card Game MEGA Expansion Pack 30th CELEBRATION [M6a] Futuristic Box"
        self.assertTrue(same_model(a, b) and same_model(a, c) and same_model(b, c))
        self.assertTrue(same_model("AULA F75", "aula-f75"))
        self.assertFalse(same_model("AULA F75", "AULA F75 Max"))
        self.assertFalse(same_model("AULA F75", "Ajazz AK820 Pro"))

    def test_grouped_once_and_collectible_skipped(self):
        from unittest import mock
        from sourcing_agent import quality
        rs = [{"name": "A", "total": 70, "model": "Pokemon Card Game 30th Celebration Futuristic Box tcg0572"},
              {"name": "B", "total": 60, "model": "Pokémon Card Game MEGA 30th CELEBRATION FUTURISTIC BOX"}]
        with mock.patch.object(quality, "assess_model", return_value={"verdict": "ok", "review_sources": 3}) as am:
            n = quality.assess_quality(None, [dict(r) for r in rs], {"product": "박스", "category": "생활"}, log=lambda m: None)
        self.assertEqual((n, am.call_count), (1, 1))
        out = [dict(r) for r in rs]
        with mock.patch.object(quality, "assess_model") as am:
            n = quality.assess_quality(None, out, {"product": "포켓몬카드 일본판 30주년 박스", "category": "취미/수집"},
                                       log=lambda m: None)
        self.assertEqual((n, am.call_count), (0, 0))
        self.assertTrue(out[0]["quality"]["skipped"] and out[0]["quality"]["sample_needed"] is False)
        self.assertNotIn("evidence", out[0])
        self.assertIn("조사 생략", quality.quality_text(out[0]["quality"]))


if __name__ == "__main__":
    unittest.main()
