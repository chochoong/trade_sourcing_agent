"""제품 사양(specs) — 2번이 판매 페이지에서 확인한 사실만 기록, 3번 통관·규제 체크가 읽음.

저장 형식 (trade_hub v13 product_suppliers.specs_json):
  {"flags": {<키>: 값 또는 null}, "materials": ["ABS", ...], "evidence": [{"claim": ..., "url": ...}]}
키·값 형식은 3번(compliance_agent/flags.py)의 FLAGS 와 같음. null = 모름 (추측으로 false 를 넣지 않음).
"""
import re

POWER_SOURCES = ("none", "ac", "usb", "battery_lithium", "battery_other")
# 모델·옵션마다 전원이 다르면 위험한 쪽(앞쪽)을 기록
POWER_RISK_ORDER = ("battery_lithium", "ac", "battery_other", "usb", "none")

SPEC_FLAGS = {
    "power_source": "enum",
    "is_electronic": "bool",
    "has_heating_or_motor": "bool",
    "wireless": "bool",
    "food_contact": "bool",
    "chemical_product": "bool",
    "hygiene_product": "bool",
    "cosmetic": "bool",
    "synthetic_resin_household": "bool",
    "textile_wearable": "bool",
    "origin_country": "str",
}

_COUNTRY_CODES = {"china": "CN", "중국": "CN", "prc": "CN", "japan": "JP", "일본": "JP", "usa": "US",
                  "united states": "US", "미국": "US", "vietnam": "VN", "베트남": "VN", "korea": "KR",
                  "south korea": "KR", "한국": "KR", "taiwan": "TW", "대만": "TW", "india": "IN", "인도": "IN",
                  "thailand": "TH", "태국": "TH", "indonesia": "ID", "인도네시아": "ID", "germany": "DE",
                  "독일": "DE", "italy": "IT", "이탈리아": "IT", "hong kong": "HK", "홍콩": "HK"}


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def _power(v):
    """문자열 하나 또는 목록(모델별로 다를 때) → 가장 위험한 값 하나."""
    vals = [str(x).strip().lower() for x in (v if isinstance(v, list) else [v]) if x is not None]
    vals = [x for x in vals if x in POWER_SOURCES]
    return next((p for p in POWER_RISK_ORDER if p in vals), None)


def _country(v):
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return s.upper()
    return _COUNTRY_CODES.get(s.lower())


def normalize_specs(raw):
    """LLM 응답의 specs → 저장 형식. 확인한 값이 하나도 없으면 None (= 3번이 '모름'으로 처리)."""
    if not isinstance(raw, dict):
        return None
    src = raw.get("flags") if isinstance(raw.get("flags"), dict) else raw
    flags = {}
    for key, kind in SPEC_FLAGS.items():
        v = src.get(key)
        flags[key] = _power(v) if kind == "enum" else _country(v) if kind == "str" else _bool(v)
    materials = [str(m).strip() for m in (raw.get("materials") or []) if isinstance(m, (str, int, float)) and str(m).strip()] \
        if isinstance(raw.get("materials"), list) else []
    evidence = []
    for e in raw.get("evidence") or [] if isinstance(raw.get("evidence"), list) else []:
        if isinstance(e, dict) and e.get("claim"):
            evidence.append({"claim": str(e["claim"]), "url": e.get("url") if isinstance(e.get("url"), str) else None})
        elif isinstance(e, str) and e.strip():
            evidence.append({"claim": e.strip(), "url": None})
    if all(v is None for v in flags.values()) and not materials:
        return None
    return {"flags": flags, "materials": materials, "evidence": evidence}
