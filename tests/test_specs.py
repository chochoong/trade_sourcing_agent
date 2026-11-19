"""제품 사양(specs) 정리: 3번 통관·규제 체크가 읽는 형식, 모르면 null."""
import unittest

from sourcing_agent.specs import SPEC_FLAGS, normalize_specs


class SpecsTest(unittest.TestCase):
    def test_keeps_only_confirmed_values(self):
        s = normalize_specs({"flags": {"power_source": "battery_lithium", "wireless": True, "food_contact": "maybe",
                                       "is_electronic": "true", "origin_country": "China", "children_target": True},
                             "materials": ["ABS", ""], "evidence": [{"claim": "리튬", "url": "https://x"}, "USB-C 충전"]})
        f = s["flags"]
        self.assertEqual(set(f), set(SPEC_FLAGS))             # 키 목록 고정, 모르는 키는 버림
        self.assertEqual(f["power_source"], "battery_lithium")
        self.assertIs(f["wireless"], True)
        self.assertIs(f["is_electronic"], True)
        self.assertIsNone(f["food_contact"])                  # 추측성 값은 null
        self.assertIsNone(f["cosmetic"])                      # 안 적었으면 null (false 아님)
        self.assertEqual(f["origin_country"], "CN")
        self.assertEqual(s["materials"], ["ABS"])
        self.assertEqual(s["evidence"][1], {"claim": "USB-C 충전", "url": None})

    def test_riskier_power_source_wins(self):
        s = normalize_specs({"flags": {"power_source": ["none", "battery_lithium", "usb"]}})
        self.assertEqual(s["flags"]["power_source"], "battery_lithium")
        self.assertIsNone(normalize_specs({"flags": {"power_source": "solar"}}))

    def test_nothing_known_is_none(self):
        self.assertIsNone(normalize_specs(None))
        self.assertIsNone(normalize_specs({"flags": {k: None for k in SPEC_FLAGS}, "materials": []}))


if __name__ == "__main__":
    unittest.main()
