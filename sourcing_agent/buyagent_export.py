"""2번 조사 결과 → 구매대행 모니터(buyagent) 카탈로그로 내보내기.

구매대행은 주문을 받은 뒤 해외 상품 페이지에서 1개씩 사서 고객 명의로 보내는 방식이라,
buyagent 에 필요한 건 '어느 상품 페이지에서 낱개를 얼마에 파는가' 입니다.

  products.csv       : 품목 1줄 (sources=manual → 수동 가격으로 원가·권장가 계산, hub_product_id = 공용 DB 품목 번호)
  manual_prices.csv  : 낱개 구매가 되는 판매처마다 1줄 (buyagent 가 그중 원가가 가장 낮은 곳을 고름)

- retail(구매대행) 조사 결과만 넘깁니다. 소량 사입 결과는 도매가라 구매대행 원가로 쓰면 안 됨
  (꼭 필요하면 allow_non_retail=True, 이때 memo 에 '소량 사입 결과(도매가)' 표시).
- 같은 (id, url) 이 이미 있으면 가격·배송비·재고·확인 날짜·memo 를 새 값으로 갱신 (줄은 늘리지 않음).
  단, 이 모듈이 넣은 줄(memo 에 MARKER)만 갱신하고 사람이 손으로 쓴 줄은 건드리지 않습니다.
- 가품·상표 도용 위험이 지적된 판매처와 점수가 너무 낮은 판매처(기본 25점 미만)는 넘기지 않습니다.
  buyagent 는 판매처 중 가장 싼 곳을 고르기 때문에, 싸고 위험한 곳이 1순위가 되는 것을 막기 위함.
- 현지 배송비는 모르면 빈 칸 (buyagent 가 '미확인'으로 처리). 무료배송이 확실할 때만 0.
- 기본은 미리보기만 하고, write=True 일 때만 파일을 씁니다.
"""
import csv
import json
import os
import re
from datetime import date
from pathlib import Path

from .normalize import is_product_url, parse_usd_price

DEFAULT_BUYAGENT = Path(os.getenv("BUYAGENT_DIR", Path(__file__).resolve().parents[2] / "buyagent" / "buyagent"))

MARKER = "2번 공급처 발굴에서 가져옴"
# MARKER 를 넣기 전(2026-11-17)에 이 모듈이 쓴 가격 줄의 memo 형식: "B등급 64점 · 현지 배송비 미확인 · ..."
_LEGACY_MEMO = re.compile(r"^[A-D-]등급 [\d.]+점 · 현지 배송비")

PRODUCT_COLS = ["id", "name", "category", "keyword", "jan", "must_include", "exclude", "trusted_shops", "sources",
                "shipping_profile", "bundle_size", "my_price_coupang", "my_price_naver", "active", "memo",
                "hub_product_id"]
PRICE_COLS = ["id", "price", "currency", "origin", "shipping", "in_stock", "shop", "url", "checked", "memo"]
UPDATE_COLS = ["price", "shipping", "in_stock", "checked", "memo"]   # 다시 내보낼 때 바꾸는 칸

# 이 점수 미만 판매처는 넘기지 않음 (환경변수 BUYAGENT_MIN_SCORE 또는 --min-score 로 변경)
DEFAULT_MIN_SCORE = float(os.getenv("BUYAGENT_MIN_SCORE", "25"))
# 검증 단계 risk_flags 에 이런 표현이 있으면 가품 위험으로 보고 제외.
# '상표'만으로는 걸지 않음 ("모방 디자인이면 상표권 침해 소지" 같은 조건부 경고까지 걸리므로)
_COUNTERFEIT = re.compile(r"도용|가품|위조|짝퉁|counterfeit|fake|replica", re.I)

# buyagent settings.toml 의 [origins.*] 에 있는 나라만 (없는 나라는 면세 한도·배대지 요금을 모름)
ORIGINS = {"china": "CN", "중국": "CN", "cn": "CN", "japan": "JP", "일본": "JP", "jp": "JP",
           "usa": "US", "united states": "US", "미국": "US", "us": "US"}

# 품목명 → buyagent 카테고리 키 (위에서부터 먼저 맞는 것). 키캡·스위치를 키보드보다 먼저 검사.
CATEGORY_HINTS = [
    ("keycap", ["keycap", "키캡"]),
    ("key_switch", ["switch tester", "key switch", "스위치"]),
    ("desk_acc", ["wrist rest", "desk mat", "mouse pad", "손목받침", "장패드", "데스크매트"]),
    ("keyboard", ["keyboard", "키보드"]),
    ("smart_wearable", ["smart glasses", "ai glasses", "smartwatch", "smart watch", "earbuds", "earphone",
                        "headphone", "안경", "스마트워치", "이어폰", "헤드폰"]),
    ("watch", ["watch", "시계"]),
    ("figure_keychain", ["keychain", "키링"]),
    ("acrylic", ["acrylic", "아크릴"]),
    ("stationery", ["notebook", "planner", "stationery", "다이어리", "문구"]),
    ("bag", ["bag", "pouch", "가방", "파우치"]),
    ("apparel", ["shirt", "hoodie", "apparel", "의류", "티셔츠"]),
    ("kitchen", ["tumbler", "kitchen", "mug", "텀블러", "주방"]),
    # 구매대행 금지 카테고리: 맞으면 내보내지 않고 사유를 알려 줌
    ("plush", ["plush", "인형"]),
    ("kids", ["baby", "kids", "toy", "유아", "아동", "장난감"]),
    ("cosmetics", ["cosmetic", "화장품"]),
    ("food", ["snack", "food", "과자", "식품"]),
    ("electronics", ["speaker", "charger", "power bank", "스피커", "충전기", "보조배터리", "전자"]),
]


def load_categories(buyagent_dir=None) -> dict:
    import tomllib
    path = (Path(buyagent_dir) if buyagent_dir else DEFAULT_BUYAGENT) / "config" / "categories.toml"
    with open(path, "rb") as f:
        return tomllib.load(f).get("categories", {})


def guess_category(request: dict, buyagent_dir=None):
    """(카테고리 키, None) 또는 (None, 못 정한 이유). buyagent 에 있고 allowed 인 카테고리만."""
    text = " ".join(str(request.get(k) or "") for k in ("product", "product_en", "category")).lower()
    cats = load_categories(buyagent_dir)
    for key, words in CATEGORY_HINTS:
        if any(w in text for w in words):
            c = cats.get(key)
            if not c:
                return None, f"buyagent 에 '{key}' 카테고리가 없음"
            if not c.get("allowed", True):
                return None, f"구매대행 금지 카테고리({c.get('name', key)}): {c.get('reason', '')}"
            return key, None
    return None, "품목명으로 buyagent 카테고리를 정하지 못함"


def product_id(request: dict) -> str:
    base = request.get("product_en") or request.get("product") or "item"
    words = re.sub(r"[^a-z0-9]+", " ", base.lower()).split()[:5]
    return "-".join(words)[:40] or "item"


def first_qty(text):
    """MOQ 원문의 첫 숫자 ("1 piece" → 1, "2 pieces" → 2, "1,000 pcs" → 1000). 없으면 None."""
    m = re.search(r"\d[\d,]*", str(text or ""))
    return int(m[0].replace(",", "")) if m else None


def shipping_value(s: dict) -> str:
    """현지 배송비: 금액을 알면 숫자, 무료배송이 확실하면 0, 모르면 빈 칸(= buyagent 에서 '미확인')."""
    val = s.get("shipping_usd")
    if isinstance(val, (int, float)) and val >= 0:
        return f"{val:g}"
    text = " ".join(str(s.get(k) or "") for k in ("shipping", "shipping_cost"))
    if re.search(r"free shipping|무료\s*배송|包邮", text, re.I):
        return "0"
    return ""


def _origin(country) -> str | None:
    return ORIGINS.get(str(country or "").strip().lower())


def is_ours(row: dict) -> bool:
    memo = row.get("memo") or ""
    return MARKER in memo or bool(_LEGACY_MEMO.match(memo))


def counterfeit_flag(s: dict):
    """가품·상표 도용 위험을 지적한 risk_flag 문장 (없으면 None)."""
    return next((str(x) for x in s.get("risk_flags") or [] if _COUNTERFEIT.search(str(x))), None)


def build_rows(payload: dict, category: str, run_name: str = "", wholesale: bool = False,
               min_score: float = DEFAULT_MIN_SCORE):
    """results.json → (products 1줄, manual_prices 여러 줄, 건너뛴 업체와 사유)."""
    req = payload.get("request") or {}
    pid = product_id(req)
    hub_id = req.get("trade_hub_product_id")
    product = {c: "" for c in PRODUCT_COLS}
    product.update(id=pid, name=req.get("product") or req.get("product_en") or pid, category=category,
                   keyword=req.get("product_en") or req.get("product") or "", sources="manual",
                   my_price_coupang="0", my_price_naver="0", active="Y",
                   memo=f"{MARKER} {run_name}".strip(), hub_product_id="" if hub_id is None else str(hub_id))

    checked = (payload.get("generated_at") or "")[:10] or date.today().isoformat()
    tag = "소량 사입 결과(도매가) · " if wholesale else ""
    prices, skipped = [], []
    for s in payload.get("suppliers") or []:
        name = s.get("name") or "?"
        url = next((u for u in s.get("urls") or [] if is_product_url(u)), None)
        price = s.get("unit_price_usd") or parse_usd_price(s.get("price_range"))
        origin = _origin(s.get("country"))
        moq = first_qty(s.get("moq"))
        fake = counterfeit_flag(s)
        total = s.get("total") or 0
        why = (f"가품·상표 도용 위험: {fake[:80]}" if fake
               else f"점수 {total:g}점 < 기준 {min_score:g}점" if total < min_score
               else "상품 상세 페이지 없음" if not url
               else "USD 단가 없음" if not price
               else f"buyagent 에 없는 나라({s.get('country')})" if not origin
               else f"MOQ {moq}개 → 낱개 구매 불가 (구매대행은 1개씩 삼)" if moq and moq > 1 and not wholesale
               else None)
        if why:
            skipped.append((name, why))
            continue
        ship = shipping_value(s)
        prices.append({"id": pid, "price": f"{price:g}", "currency": "USD", "origin": origin,
                       "shipping": ship, "in_stock": "Y", "shop": name, "url": url, "checked": checked,
                       "memo": f"{tag}{s.get('grade') or '-'}등급 {s.get('total') or 0:g}점 · "
                               f"{'' if ship else '현지 배송비 미확인 · '}가격대 {s.get('price_range') or '-'} · "
                               f"{MARKER} {run_name}".strip()})
    return product, prices, skipped


def _load(path: Path):
    """(헤더, 줄 목록, BOM 여부). 파일이 없으면 (None, [], False)."""
    if not path.exists():
        return None, [], False
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows, bom


def _save(path: Path, header: list[str], rows: list[dict], bom: bool) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) or "" for c in header})


def export(run_dir, category: str | None = None, buyagent_dir=None, write: bool = False,
           allow_non_retail: bool = False, min_score: float | None = None) -> dict:
    """category 를 생략하면 품목명으로 추정. 넘길 수 없는 경우(retail 아님, 카테고리 미정 등)는 ValueError."""
    run_dir = Path(run_dir)
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    stype = payload.get("sourcing_type") or "oem"
    wholesale = stype != "retail"
    if wholesale and not allow_non_retail:
        raise ValueError(f"구매 방식이 retail 이 아님({stype}) → 도매가라 구매대행 원가로 쓸 수 없어 내보내지 않음. "
                         f"retail 로 다시 조사하거나, 꼭 필요하면 --allow-non-retail")
    ba = Path(buyagent_dir) if buyagent_dir else DEFAULT_BUYAGENT
    if not category:
        category, why = guess_category(payload.get("request") or {}, ba)
        if not category:
            raise ValueError(f"{why} → --category 로 지정하세요")
    products_csv, prices_csv = ba / "catalog" / "products.csv", ba / "catalog" / "manual_prices.csv"
    if not products_csv.exists():
        raise FileNotFoundError(f"buyagent 카탈로그를 찾지 못했습니다: {products_csv} (--buyagent 로 경로 지정)")

    min_score = DEFAULT_MIN_SCORE if min_score is None else min_score
    product, prices, skipped = build_rows(payload, category, run_dir.name, wholesale, min_score)
    warnings = []
    p_header, p_rows, p_bom = _load(products_csv)
    q_header, q_rows, q_bom = _load(prices_csv)
    q_header = q_header or PRICE_COLS
    if "hub_product_id" not in p_header:
        warnings.append("buyagent products.csv 에 hub_product_id 열이 없어 품목 번호를 쓰지 못함 (기존 열만 씀)")

    # 품목: 없으면 추가 (판매처가 하나도 없으면 추가하지 않음). 있으면 비어 있는 hub_product_id 만 채움.
    product_action = "none"
    existing = next((r for r in p_rows if r.get("id") == product["id"]), None)
    if existing is None:
        if prices:
            p_rows.append(product)
            product_action = "add"
    elif ("hub_product_id" in p_header and not existing.get("hub_product_id")
          and product["hub_product_id"] and is_ours(existing)):
        existing["hub_product_id"] = product["hub_product_id"]
        product_action = "update"

    # 판매처: 같은 (id, url) 이 있으면 이 모듈이 넣은 줄만 갱신, 없으면 추가
    index = {(r.get("id"), r.get("url")): r for r in q_rows}
    added, updated, same, kept_manual = [], [], [], []
    for new in prices:
        old = index.get((new["id"], new["url"]))
        if old is None:
            q_rows.append(new)
            added.append(new)
        elif not is_ours(old):
            kept_manual.append(new["shop"])
        elif all((old.get(c) or "") == new[c] for c in UPDATE_COLS):
            same.append(new["shop"])
        else:
            changes = {c: [old.get(c) or "", new[c]] for c in UPDATE_COLS if (old.get(c) or "") != new[c]}
            old.update({c: new[c] for c in UPDATE_COLS})
            updated.append({"shop": new["shop"], "changes": changes})

    if write:
        if product_action != "none":
            _save(products_csv, p_header, p_rows, p_bom)
        if added or updated:
            _save(prices_csv, q_header, q_rows, q_bom)
    return {"written": write, "buyagent": str(ba), "product_id": product["id"], "category": category,
            "hub_product_id": product["hub_product_id"] or None, "sourcing_type": stype,
            "product": product_action,
            "summary": f"추가 {len(added)}줄 / 갱신 {len(updated)}줄 / 그대로 {len(same)}줄"
                       + (f" / 손으로 쓴 줄이라 안 건드림 {len(kept_manual)}줄" if kept_manual else ""),
            "added": added, "updated": updated, "unchanged": same, "kept_manual": kept_manual,
            "skipped": [{"name": n, "reason": r} for n, r in skipped], "warnings": warnings}
