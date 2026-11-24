"""공식 마켓 API (AliExpress·eBay): 응답 해석, 국가별 선택, 실패 시 건너뜀, 검증 결과에 포장·무게 채우기.
실제 네트워크 호출 없음 — 2026-11-24 실제 응답에서 확인한 필드 구조로 만든 가짜 응답을 씀."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sourcing_agent import marketplaces as mk

ALI_SEARCH = {"aliexpress_ds_text_search_response": {"code": "00", "data": {"totalCount": 30996, "products": {
    "selection_search_product": [
        {"itemId": "1005013294919399", "title": "Keycap Clicker Keychain", "targetSalePrice": "2.83",
         "itemUrl": "//www.aliexpress.com/item/1005013294919399.html?sku=1", "orders": "26", "score": "5.0"},
        {"itemId": "1005012774894491", "title": "Keycap Fidget", "targetSalePrice": "5.39",
         "itemUrl": "//www.aliexpress.com/item/1005012774894491.html", "orders": "107"},
        {"itemId": "1", "title": "가격 없음", "targetSalePrice": "", "itemUrl": "//x"}]}}}}

ALI_DETAIL = {"aliexpress_ds_product_get_response": {"result": {
    "ae_store_info": {"store_id": 1104064824, "store_name": "Shop1104064824 Store", "store_country_code": "CN",
                      "item_as_described_rating": "4.6", "communication_rating": "4.7", "shipping_speed_rating": "4.7"},
    "package_info_dto": {"package_width": 11, "package_height": 3, "package_length": 14, "gross_weight": "0.122"},
    "ae_item_base_info_dto": {"sales_count": "26", "evaluation_count": "5", "avg_evaluation_rating": "5.0"},
    "ae_item_properties": {"ae_item_property": [{"attr_name": "Material", "attr_value": "ABS"},
                                                {"attr_name": "Origin", "attr_value": "Mainland China"}]}}}}

EBAY_SEARCH = {"total": 458, "itemSummaries": [
    {"itemId": "v1|123456789012|0", "title": "Keycap Keychain", "price": {"value": "7.99", "currency": "USD"},
     "seller": {"username": "amteel11", "feedbackPercentage": "98.2", "feedbackScore": 570},
     "itemWebUrl": "https://www.ebay.com/itm/123456789012?hash=x", "itemLocation": {"country": "US"}}]}

EBAY_DETAIL = {"estimatedAvailabilities": [{"estimatedSoldQuantity": 8}], "material": "Plastic",
               "itemWebUrl": "https://www.ebay.com/itm/123456789012",
               "localizedAspects": [{"name": "Country of Origin", "value": "China"},
                                    {"name": "Item Weight", "value": "1.2 oz"}]}


class ParseTest(unittest.TestCase):
    def test_ali(self):
        total, items = mk.parse_ali_search(ALI_SEARCH)
        self.assertEqual(total, 30996)
        self.assertEqual([i["price_usd"] for i in items], [2.83, 5.39])        # 가격 없는 상품 제외
        self.assertEqual(items[0]["url"], "https://www.aliexpress.com/item/1005013294919399.html")
        d = mk.parse_ali_detail(ALI_DETAIL, items[0]["url"])
        self.assertEqual(d["store"]["name"], "Shop1104064824 Store")
        self.assertEqual(d["store"]["rating_described"], 4.6)
        self.assertEqual((d["sold"], d["reviews"], d["material"], d["origin"]), (26, 5, "ABS", "Mainland China"))
        self.assertEqual(d["packaging"]["per_unit_kg"], 0.122)                # → 4번 운임 추정
        self.assertEqual(d["packaging"]["unit_size_cm"], [14, 11, 3])
        self.assertIn("AliExpress API", d["packaging"]["evidence"][0]["claim"])

    def test_ali_error(self):
        with self.assertRaises(mk.MarketError):
            mk.parse_ali_search({"aliexpress_ds_text_search_response": {"code": "4001", "msg": "bad"}})

    def test_ebay(self):
        total, items = mk.parse_ebay_search(EBAY_SEARCH)
        self.assertEqual((total, items[0]["price_usd"]), (458, 7.99))
        self.assertEqual(items[0]["store"], {"name": "amteel11", "feedback_pct": 98.2, "feedback_score": 570})
        self.assertEqual(items[0]["url"], "https://www.ebay.com/itm/123456789012")
        d = mk.parse_ebay_detail(EBAY_DETAIL)
        self.assertEqual((d["sold"], d["material"], d["origin"]), (8, "Plastic", "China"))
        self.assertAlmostEqual(d["packaging"]["per_unit_kg"], 0.034, places=3)


class SnapshotTest(unittest.TestCase):
    def test_china_uses_aliexpress(self):
        def call(method, params):
            return ALI_SEARCH if method.endswith("text.search") else ALI_DETAIL
        with mock.patch.object(mk, "ali_call", side_effect=call):
            s = mk.market_snapshot({"product_en": "keycap keychain", "preferred_countries": ["China"]}, log=lambda m: None)
        self.assertEqual(s["source"], "AliExpress")
        self.assertEqual(s["price_usd"], {"min": 2.83, "median": 4.11, "max": 5.39})
        self.assertEqual(s["listings"][0]["sold"], 26)                       # 상세 조회의 판매 수가 검색 값보다 우선
        self.assertEqual(s["listings"][0]["store"]["name"], "Shop1104064824 Store")
        c = mk.compact(s)
        self.assertNotIn("id", c["listings"][0].get("store", {}))
        self.assertEqual(c["listings"][0]["packaging"]["per_unit_kg"], 0.122)

    def test_usa_uses_ebay(self):
        def get(path, params=None):
            return EBAY_SEARCH if path.startswith("item_summary") else EBAY_DETAIL
        with mock.patch.object(mk, "ebay_get", side_effect=get):
            s = mk.market_snapshot({"product_en": "keycap", "preferred_countries": ["USA"]}, log=lambda m: None)
        self.assertEqual((s["source"], s["listings"][0]["sold"]), ("eBay", 8))

    def test_japan_or_failure_skips(self):
        logs = []
        self.assertIsNone(mk.market_snapshot({"product_en": "x", "preferred_countries": ["Japan"]}, log=logs.append))
        with mock.patch.object(mk, "ali_call", side_effect=mk.MarketError("로그인 토큰 만료")):
            self.assertIsNone(mk.market_snapshot({"product_en": "x", "preferred_countries": ["China"]},
                                                 log=logs.append))
        self.assertIn("건너뜀", logs[-1])                                     # 조사는 계속 (None)

    def test_missing_keys_readable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "ALIEXPRESS_APP_KEY": "k", "ALIEXPRESS_APP_SECRET": "s",
                "ALIEXPRESS_TOKEN_FILE": str(Path(d) / "none.json")}):
            with self.assertRaisesRegex(mk.MarketError, "토큰 파일 없음"):
                mk._ali_auth()
            (Path(d) / "t.json").write_text(json.dumps({"access_token": "x", "expire_time": 1}), encoding="utf-8")
            os.environ["ALIEXPRESS_TOKEN_FILE"] = str(Path(d) / "t.json")
            with self.assertRaisesRegex(mk.MarketError, "만료"):
                mk._ali_auth()


class EnrichTest(unittest.TestCase):
    def test_fills_packaging_for_same_item(self):
        with mock.patch.object(mk, "ali_call", side_effect=lambda m, p: ALI_SEARCH if "search" in m else ALI_DETAIL):
            snap = mk.ali_snapshot("keycap")
        r = mk.enrich({"urls": ["https://www.aliexpress.com/item/1005013294919399.html"], "packaging": None}, snap)
        self.assertEqual(r["packaging"]["per_unit_kg"], 0.122)
        self.assertEqual(r["market_listing"]["source"], "AliExpress")
        other = mk.enrich({"urls": ["https://www.alibaba.com/product-detail/x_1601010059862.html"]}, snap)
        self.assertNotIn("market_listing", other)                            # 다른 상품은 그대로
        kept = mk.enrich({"urls": ["https://www.aliexpress.com/item/1005013294919399.html"],
                          "packaging": {"per_unit_kg": 0.5}}, snap)
        self.assertEqual(kept["packaging"]["per_unit_kg"], 0.5)               # 검증에서 찾은 값이 우선

    def test_ebay_url_ids(self):
        self.assertEqual(mk._item_ids(["https://www.ebay.com/itm/Keycap-Keychain/123456789012"]), {"123456789012"})


if __name__ == "__main__":
    unittest.main()
