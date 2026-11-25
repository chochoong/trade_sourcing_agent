"""가격 기준 (price_basis) — 공급처 단가가 어떤 조건의 가격인지. 4번 원가 계산이 읽음.

저장 위치: 공용 DB product_suppliers.trade_terms_json 의 "price_basis" (DB 구조 변경 없음)
  {"currency": "JPY", "low": 21000, "high": 42000,   원래 통화와 단가 범위 (요청 수량 구간)
   "fx_assumed": 154,                                USD 환산에 쓴 환율 (1 USD = ? 원래 통화)
   "includes_shipping": false,                       배송비 포함 가격인지 (알리 무료배송 등이면 true)
   "includes_tax": true,                             한국 관부가세까지 포함(DDP)일 때만 true — 판매국 세금(일본 소비세 등)은 해당 없음
   "preorder": true,
   "kind": "factory|wholesale|retail|resale",       공장 / 도매 / 소매 / 리셀
   "variant": "4구 LED형",                           이 단가가 가리키는 상품 (모델·구성·규격)
   "tiers": [{"min_qty": 2, "price": 13.9}, {"min_qty": 100, "price": 11.5}],   수량 구간별 단가 (원래 통화)
                                                     → 4번이 몇 개를 주문해야 가장 이익인지 계산
   "evidence": [{"claim": ..., "url": ...}]}
확인한 값만 넣고 모르는 키는 아예 뺌. 확인한 값이 하나도 없으면 None (저장 안 함).
unit_price_usd 는 지금처럼 범위의 높은 쪽(USD) — 여기와 별개.
"""
import re

KINDS = ("factory", "wholesale", "retail", "resale")
_KIND_ALIASES = {"manufacturer": "factory", "공장": "factory", "도매": "wholesale", "소매": "retail",
                 "리셀": "resale"}
_BOOL_KEYS = ("includes_shipping", "includes_tax", "preorder")
_CURRENCY_ALIASES = {"$": "USD", "US$": "USD", "RMB": "CNY", "元": "CNY", "¥": None, "円": "JPY", "엔": "JPY",
                     "위안": "CNY", "원": "KRW", "€": "EUR"}


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"\d+(?:\.\d+)?", v.replace(",", ""))
        if m:
            x = float(m.group(0))
            return (int(x) if x.is_integer() else x) if x > 0 else None
    return None


def _bool(v):
    """true / false 만 (문자열 "true"·"false" 도 받음). 그 밖은 모름 → None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "포함"):
            return True
        if s in ("false", "no", "미포함", "별도"):
            return False
    return None


def _currency(v):
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[s]
    s = s.upper()
    return s if re.fullmatch(r"[A-Z]{3}", s) else None


def kind_of(v):
    s = str(v or "").strip().lower()
    s = _KIND_ALIASES.get(s, s)
    return s if s in KINDS else None


_HOST_KINDS = (
    (("aliexpress.", "ebay.", "amazon.", "rakuten.", "shopping.yahoo.", "temu.", "coupang.", "smartstore."), "retail"),
    (("mercari.", "fril.jp", "kream.", "bunjang.", "stockx."), "resale"),
    (("alibaba.com", "1688.com", "made-in-china.", "globalsources.", "dhgate.", "netsea.", "superdelivery.", "tradekorea."),
     "wholesale"),
)


def kind_from_urls(urls, company_type=None):
    """상품 페이지 주소로 가격 종류 추정. 도매 플랫폼의 제조사면 factory. 모르면 None. (첫 주소 = 상품 상세 우선)"""
    for u in [str(x).lower() for x in urls or [] if x]:
        for hosts, kind in _HOST_KINDS:
            if any(h in u for h in hosts):
                if kind == "wholesale" and str(company_type or "").lower() in ("manufacturer", "제조사"):
                    return "factory"
                return kind
    return None


def normalize_tiers(v):
    """[{"min_qty": 100, "price": 11.5}, …] → 수량 오름차순, 같은 수량은 하나만, 최대 8구간. 모르면 None."""
    if not isinstance(v, list):
        return None
    by_qty = {}
    for t in v:
        if not isinstance(t, dict):
            continue
        q, p = _num(t.get("min_qty")), _num(t.get("price"))
        if q and p:
            by_qty.setdefault(int(q), p)
    tiers = [{"min_qty": q, "price": by_qty[q]} for q in sorted(by_qty)][:8]
    return tiers or None


def normalize_price_basis(v):
    if not isinstance(v, dict):
        return None
    out = {}
    cur = _currency(v.get("currency"))
    if cur:
        out["currency"] = cur
    low, high = _num(v.get("low")), _num(v.get("high"))
    if low and high and low > high:
        low, high = high, low
    if low or high:
        out["low"], out["high"] = low or high, high or low
    fx = _num(v.get("fx_assumed"))
    if fx and out.get("currency") not in (None, "USD"):
        out["fx_assumed"] = fx
    for k in _BOOL_KEYS:
        b = _bool(v.get(k))
        if b is not None:
            out[k] = b
    if out.get("includes_tax") is not True:   # 4번은 true 를 'DDP(한국 관부가세 포함)'로 읽음 → 그 밖은 키 없음
        out.pop("includes_tax", None)
    kind = kind_of(v.get("kind"))
    if kind:
        out["kind"] = kind
    variant = str(v.get("variant") or "").strip()
    if variant and variant.lower() not in ("null", "none"):
        out["variant"] = variant[:80]
    tiers = normalize_tiers(v.get("tiers"))
    if tiers:
        out["tiers"] = tiers
    if not out:
        return None
    ev = [{"claim": str(e.get("claim") or ""), "url": e.get("url")} for e in (v.get("evidence") or [])
          if isinstance(e, dict) and e.get("claim")] if isinstance(v.get("evidence"), list) else []
    if ev:
        out["evidence"] = ev
    return out


def from_market(listing, source):
    """공식 마켓 API 상품 → 가격 기준 (USD 소매가, 받은 값만)."""
    if not isinstance(listing, dict) or not _num(listing.get("price_usd")):
        return None
    p = listing["price_usd"]
    return {"currency": "USD", "low": p, "high": p, "kind": "retail",
            "evidence": [{"claim": f"{source} 공식 API 판매가 USD {p:g}", "url": listing.get("url")}]}


def merge(base, extra):
    """base(검증 결과) 에 없는 키만 extra(API 등) 로 채움."""
    if not extra:
        return base
    if not base:
        return dict(extra)
    out = dict(base)
    for k, val in extra.items():
        if k == "evidence":
            out["evidence"] = (out.get("evidence") or []) + [e for e in val if e not in (out.get("evidence") or [])]
        elif k not in out:
            out[k] = val
    return out


def price_basis_text(b):
    """리포트 한 줄: 'JPY 21,000~42,000 · 배송비 별도 · 소매 · 예약판매 — 4구 LED형'."""
    if not isinstance(b, dict) or not b:
        return ""
    parts = []
    if b.get("low"):
        rng = f"{b['low']:,}" if b["low"] == b.get("high") else f"{b['low']:,}~{b['high']:,}"
        parts.append(f"{b.get('currency') or ''} {rng}".strip())
    if "includes_shipping" in b:
        parts.append("배송비 포함" if b["includes_shipping"] else "배송비 별도")
    if b.get("includes_tax"):
        parts.append("한국 관부가세 포함(DDP)")
    if b.get("tiers"):
        parts.append(f"{len(b['tiers'])}구간 (≥{b['tiers'][-1]['min_qty']:,}개 {b['tiers'][-1]['price']:g})")
    if b.get("kind"):
        parts.append({"factory": "공장가", "wholesale": "도매", "retail": "소매", "resale": "리셀"}[b["kind"]])
    if b.get("preorder"):
        parts.append("예약판매")
    text = " · ".join(parts)
    return f"{text} — {b['variant']}" if b.get("variant") else text
