"""포장·무게 (packaging) — 2번이 공급처 페이지에서 확인한 값만 기록, 4번 원가·수익 계산이 운임 추정에 씀.

저장 위치: 공용 DB product_suppliers.trade_terms_json 의 "packaging" (2번이 쓰는 칸이라 DB 구조 변경 없음)
  {"unit_weight_kg": 0.35,          개당 무게 (포장 포함 무게가 있으면 그 값)
   "unit_size_cm": [30, 10, 5],     개별 포장 크기 (가로·세로·높이)
   "pcs_per_carton": 50,            카톤(수출 박스) 1개에 든 수량
   "carton_weight_kg": 18.5,        카톤 1개 총무게
   "carton_size_cm": [60, 40, 35],  카톤 크기
   "per_unit_kg": 0.37,             개당 운임용 무게 = 카톤 총무게 ÷ 카톤당 수량 (없으면 개당 무게)
   "per_unit_cbm": 0.00168,         개당 부피(㎥) = 카톤 부피 ÷ 카톤당 수량 (없으면 개별 포장 부피)
   "evidence": [{"claim": ..., "url": ...}]}
모르는 값은 null. 확인한 값이 하나도 없으면 None (저장 안 함 → 예전 값 유지).
"""
import re

_WEIGHT_UNITS = {"kg": 1.0, "kgs": 1.0, "킬로": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001, "그램": 0.001,
                 "lb": 0.453592, "lbs": 0.453592, "pound": 0.453592, "pounds": 0.453592, "oz": 0.0283495}
_LEN_UNITS = {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54}
_NUM = r"(\d+(?:\.\d+)?)"


def _pos(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def weight_kg(v):
    """0.35 / "350g" / "0.35 kg" / "1.2 lbs" → kg (숫자만 오면 kg 로 봄). 모르면 None."""
    if _pos(v):
        return round(float(v), 4)
    if not isinstance(v, str):
        return None
    m = re.search(_NUM + r"\s*(kgs?|grams?|g|lbs?|pounds?|oz|킬로|그램)?\b", v.strip().lower().replace(",", ""))
    if not m:
        return None
    kg = float(m.group(1)) * _WEIGHT_UNITS.get(m.group(2) or "kg", 1.0)
    return round(kg, 4) if kg > 0 else None


def size_cm(v):
    """[30, 10, 5] / "30x10x5 cm" / "300*100*50mm" / "12 x 4 x 2 inches" → [가로, 세로, 높이] cm. 모르면 None."""
    if isinstance(v, (list, tuple)):
        nums = [float(x) for x in v if _pos(x)]
        return [round(x, 2) for x in nums] if len(nums) == 3 else None
    if not isinstance(v, str):
        return None
    s = v.strip().lower().replace(",", "")
    nums = re.findall(_NUM, s)
    if len(nums) < 3:
        return None
    unit = re.search(r"(mm|cm|inches|inch|in|m)\b|\"", s)
    k = _LEN_UNITS.get(unit.group(0) if unit else "cm", 1.0)
    dims = [round(float(x) * k, 2) for x in nums[:3]]
    return dims if all(d > 0 for d in dims) else None


def _count(v):
    if _pos(v):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"\d+", v.replace(",", ""))
        return (int(m.group(0)) or None) if m else None
    return None


def _cbm(dims):
    return dims[0] * dims[1] * dims[2] / 1_000_000 if dims else None


def normalize_packaging(raw):
    """LLM 응답의 packaging → 저장 형식 (단위 통일 + 개당 무게·부피 계산). 확인한 값이 없으면 None."""
    if not isinstance(raw, dict):
        return None
    out = {
        "unit_weight_kg": weight_kg(raw.get("unit_weight_kg") or raw.get("unit_weight")),
        "unit_size_cm": size_cm(raw.get("unit_size_cm") or raw.get("unit_size")),
        "pcs_per_carton": _count(raw.get("pcs_per_carton")),
        "carton_weight_kg": weight_kg(raw.get("carton_weight_kg") or raw.get("carton_weight")),
        "carton_size_cm": size_cm(raw.get("carton_size_cm") or raw.get("carton_size")),
    }
    if all(v is None for v in out.values()):
        return None
    pcs = out["pcs_per_carton"]
    per_kg = out["carton_weight_kg"] / pcs if pcs and out["carton_weight_kg"] else out["unit_weight_kg"]
    per_cbm = _cbm(out["carton_size_cm"]) / pcs if pcs and out["carton_size_cm"] else _cbm(out["unit_size_cm"])
    out["per_unit_kg"] = round(per_kg, 4) if per_kg else None
    out["per_unit_cbm"] = round(per_cbm, 6) if per_cbm else None
    evidence = []
    for e in raw.get("evidence") if isinstance(raw.get("evidence"), list) else []:
        if isinstance(e, dict) and e.get("claim"):
            evidence.append({"claim": str(e["claim"]), "url": e.get("url") if isinstance(e.get("url"), str) else None})
        elif isinstance(e, str) and e.strip():
            evidence.append({"claim": e.strip(), "url": None})
    out["evidence"] = evidence
    return out


def packaging_text(p):
    """리포트 한 줄 요약. 없으면 빈 문자열."""
    if not isinstance(p, dict):
        return ""
    parts = []
    if p.get("per_unit_kg"):
        parts.append(f"개당 {p['per_unit_kg'] * 1000:.0f}g" if p["per_unit_kg"] < 1 else f"개당 {p['per_unit_kg']:g}kg")
    if p.get("unit_size_cm"):
        parts.append("포장 " + "×".join(f"{d:g}" for d in p["unit_size_cm"]) + "cm")
    if p.get("pcs_per_carton"):
        c = f"카톤 {p['pcs_per_carton']}개"
        if p.get("carton_weight_kg"):
            c += f" · {p['carton_weight_kg']:g}kg"
        if p.get("carton_size_cm"):
            c += " · " + "×".join(f"{d:g}" for d in p["carton_size_cm"]) + "cm"
        parts.append(c)
    return " / ".join(parts)
