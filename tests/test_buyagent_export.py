"""export-buyagent: retail 만, 낱개 구매만, hub_product_id, 다시 내보내면 갱신, 손으로 쓴 줄 보호."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from sourcing_agent.buyagent_export import MARKER, export

CATEGORIES = """
[categories.smart_wearable]
name = "스마트 웨어러블"
allowed = true
[categories.electronics]
name = "전자제품"
allowed = false
reason = "KC 대상"
"""
P_HEADER = ("id,name,category,keyword,jan,must_include,exclude,trusted_shops,sources,shipping_profile,"
            "bundle_size,my_price_coupang,my_price_naver,active,memo,hub_product_id")
Q_HEADER = "id,price,currency,origin,shipping,in_stock,shop,url,checked,memo"
PID = "ai-smart-glasses-with-camera"


def supplier(name, url, price, moq="1 piece", **kw):
    return {"name": name, "country": "China", "urls": [url] if url else [], "unit_price_usd": price,
            "price_range": f"USD {price}" if price else None, "moq": moq, "grade": "B", "total": 70, **kw}


SUPPLIERS = [
    supplier("A", "https://www.aliexpress.com/item/1.html", 20),
    supplier("B", "https://www.alibaba.com/product-detail/x_2.html", 15, moq="2 pieces"),
    supplier("C", "https://c.en.alibaba.com/", 18),
    supplier("D", "https://www.aliexpress.com/item/4.html", 25, shipping="Free shipping to Korea"),
]


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ba = root / "buyagent"
        (self.ba / "config").mkdir(parents=True)
        (self.ba / "catalog").mkdir()
        (self.ba / "config" / "categories.toml").write_text(CATEGORIES, encoding="utf-8")
        (self.ba / "catalog" / "products.csv").write_text(
            P_HEADER + "\nex,예시,smart_wearable,ex,,,,,manual,,,0,0,Y,예시,\n", encoding="utf-8-sig")
        (self.ba / "catalog" / "manual_prices.csv").write_text(
            Q_HEADER + "\nex,10,USD,CN,,Y,손으로 쓴 줄,https://manual.example/1,2026-11-01,사람이 적음\n",
            encoding="utf-8-sig")
        self.run = root / "20261118_000000_ai_smart_glasses"
        self.run.mkdir()
        self.write_results("retail", SUPPLIERS)

    def tearDown(self):
        self.tmp.cleanup()

    def write_results(self, stype, suppliers, product_en="AI smart glasses with camera", product="AI안경"):
        payload = {"sourcing_type": stype, "generated_at": "2026-11-18T09:00:00+09:00",
                   "request": {"product": product, "product_en": product_en, "trade_hub_product_id": 23},
                   "suppliers": suppliers}
        (self.run / "results.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def rows(self, name):
        with open(self.ba / "catalog" / name, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def append_price(self, line):
        with open(self.ba / "catalog" / "manual_prices.csv", "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")

    def test_non_retail_refused_unless_allowed(self):
        self.write_results("small_batch", SUPPLIERS)
        with self.assertRaisesRegex(ValueError, "retail"):
            export(self.run, buyagent_dir=self.ba)
        r = export(self.run, buyagent_dir=self.ba, allow_non_retail=True)
        self.assertTrue(all("소량 사입 결과(도매가)" in x["memo"] for x in r["added"]))
        self.assertIn("B", [x["shop"] for x in r["added"]])   # 도매 허용 시 MOQ 2 도 넘김

    def test_preview_does_not_write(self):
        before = (self.ba / "catalog" / "manual_prices.csv").read_bytes()
        r = export(self.run, buyagent_dir=self.ba)
        self.assertEqual(r["summary"], "추가 2줄 / 갱신 0줄 / 그대로 0줄")
        self.assertEqual((self.ba / "catalog" / "manual_prices.csv").read_bytes(), before)
        reasons = {s["name"]: s["reason"] for s in r["skipped"]}
        self.assertIn("낱개 구매 불가", reasons["B"])
        self.assertIn("상품 상세 페이지 없음", reasons["C"])

    def test_write_then_update(self):
        export(self.run, buyagent_dir=self.ba, write=True)
        prod = [p for p in self.rows("products.csv") if p["id"] != "ex"]
        self.assertEqual(len(prod), 1)
        self.assertEqual(prod[0]["hub_product_id"], "23")
        prices = {r["shop"]: r for r in self.rows("manual_prices.csv")}
        self.assertEqual(prices["A"]["shipping"], "")       # 모르면 빈 칸
        self.assertEqual(prices["D"]["shipping"], "0")      # 무료배송 확실
        self.assertIn(MARKER, prices["A"]["memo"])

        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertEqual(r["summary"], "추가 0줄 / 갱신 0줄 / 그대로 2줄")

        self.write_results("retail", [dict(SUPPLIERS[0], unit_price_usd=22)] + SUPPLIERS[1:])
        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertEqual(r["summary"], "추가 0줄 / 갱신 1줄 / 그대로 1줄")
        self.assertEqual(r["updated"][0]["changes"]["price"], ["20", "22"])
        rows = self.rows("manual_prices.csv")
        self.assertEqual(len(rows), 3)                       # 손으로 쓴 줄 + A + D (줄은 안 늘어남)
        self.assertEqual({x["shop"]: x for x in rows}["A"]["price"], "22")
        self.assertEqual(rows[0]["shop"], "손으로 쓴 줄")
        self.assertTrue((self.ba / "catalog" / "manual_prices.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_manual_row_not_overwritten(self):
        self.write_results("retail", [supplier("M", "https://www.aliexpress.com/item/9.html", 30)])
        self.append_price(f"{PID},28,USD,CN,3,Y,내가 확인,https://www.aliexpress.com/item/9.html,2026-11-10,직접 확인")
        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertEqual(r["kept_manual"], ["M"])
        row = [x for x in self.rows("manual_prices.csv") if x["shop"] == "내가 확인"][0]
        self.assertEqual(row["price"], "28")

    def test_legacy_rows_are_updated(self):
        self.append_price(f"{PID},19,USD,CN,,Y,A,https://www.aliexpress.com/item/1.html,2026-11-17,"
                          "B등급 64점 · 현지 배송비 미확인 · 가격대 USD 19")
        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertEqual(len(r["updated"]), 1)

    def test_missing_hub_column_warns(self):
        old = P_HEADER.rsplit(",", 1)[0]
        (self.ba / "catalog" / "products.csv").write_text(old + "\n", encoding="utf-8")
        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertTrue(r["warnings"])
        with open(self.ba / "catalog" / "products.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], old.split(","))
        self.assertTrue(all(len(x) == len(rows[0]) for x in rows))

    def test_no_sellers_adds_no_product(self):
        self.write_results("retail", [SUPPLIERS[1], SUPPLIERS[2]])
        r = export(self.run, buyagent_dir=self.ba, write=True)
        self.assertEqual(r["product"], "none")
        self.assertEqual(len(self.rows("products.csv")), 1)

    def test_counterfeit_and_low_score_excluded(self):
        self.write_results("retail", [
            supplier("F", "https://www.aliexpress.com/item/5.html", 11,
                     risk_flags=["Lenovo 브랜드명을 씀. 상표 도용·가품 가능성이 높음"]),
            supplier("L", "https://www.aliexpress.com/item/6.html", 12, total=13),
            supplier("OK", "https://www.aliexpress.com/item/7.html", 30,
                     risk_flags=["메타 모방 디자인이면 상표권 침해 소지"]),   # 조건부 경고는 통과
        ])
        r = export(self.run, buyagent_dir=self.ba)
        self.assertEqual([x["shop"] for x in r["added"]], ["OK"])
        reasons = {s["name"]: s["reason"] for s in r["skipped"]}
        self.assertIn("가품", reasons["F"])
        self.assertIn("기준 25점", reasons["L"])
        r = export(self.run, buyagent_dir=self.ba, min_score=10)
        self.assertEqual(sorted(x["shop"] for x in r["added"]), ["L", "OK"])

    def test_banned_category(self):
        self.write_results("retail", SUPPLIERS, product_en="bluetooth speaker", product="블루투스 스피커")
        with self.assertRaisesRegex(ValueError, "금지"):
            export(self.run, buyagent_dir=self.ba)


if __name__ == "__main__":
    unittest.main()
