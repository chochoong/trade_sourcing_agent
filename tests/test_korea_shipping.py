"""한국 직배송(해외 발송) 판매자 우선: 1차 필터 가점, 검증 결과 값 정리, 리포트 표시, 일본 조사 지시."""
import unittest

from sourcing_agent import hub
from sourcing_agent.discover import prefilter, ships_to_korea
from sourcing_agent.report import _facts, _korea_ship
from sourcing_agent.verify import TRADE_TERM_KEYS, _merge


def cand(name, ship):
    return {"name": name, "urls": [f"https://item.rakuten.co.jp/shop/{name}/"], "moq": "1",
            "trade_terms": {"ships_to_korea": ship}}


class KoreaShippingTest(unittest.TestCase):
    def test_values(self):
        self.assertIs(ships_to_korea(True), True)
        self.assertIs(ships_to_korea("true"), True)
        self.assertIs(ships_to_korea("불가"), False)
        self.assertIsNone(ships_to_korea("확인 필요"))
        self.assertIsNone(ships_to_korea(None))

    def test_direct_shipping_first(self):
        cs = [cand("domestic", False), cand("unknown", None), cand("direct", "true")]
        for mode in ("retail", "small_batch"):
            names = [c["name"] for c in prefilter(cs, {"sourcing_type": mode, "shortlist_size": 3})]
            self.assertEqual(names, ["direct", "unknown", "domestic"])        # 국내배송만도 빠지지는 않음
        oem = [c["name"] for c in prefilter(cs, {"sourcing_type": "oem", "shortlist_size": 3})]
        self.assertEqual(oem, ["domestic", "unknown", "direct"])              # oem 은 영향 없음 (원래 순서)

    def test_verify_merge_keeps_shipping(self):
        self.assertIn("ships_to_korea", TRADE_TERM_KEYS)
        m = _merge(TRADE_TERM_KEYS, {"ships_to_korea": None},
                   {"ships_to_korea": "true", "korea_shipping": "EMS 2,000엔, 7~10일"})
        self.assertEqual((m["ships_to_korea"], m["korea_shipping"]), (True, "EMS 2,000엔, 7~10일"))
        self.assertIsNone(_merge(TRADE_TERM_KEYS, {}, {"ships_to_korea": "모름"})["ships_to_korea"])

    def test_report(self):
        self.assertEqual(_korea_ship({"ships_to_korea": True, "korea_shipping": "EMS"}), "직배송 가능 (EMS)")
        self.assertEqual(_korea_ship({"ships_to_korea": False}), "국내배송만 — 배송대행지 필요")
        self.assertEqual(_korea_ship({}), "")
        rows = dict(_facts({"trade_terms": {"ships_to_korea": True}}))
        self.assertEqual(rows["한국 배송"], "직배송 가능")

    def test_japan_notes_prefer_overseas_shipping(self):
        self.assertIn("海外発送", hub.COUNTRY_NOTES["Japan"])
        self.assertIn("ships_to_korea=false", hub.COUNTRY_NOTES["Japan"])


if __name__ == "__main__":
    unittest.main()
