"""② 검색: 검색엔진 경유로 후보 업체 수집 / ③ 1차 필터."""
import re

import yaml

from .paths import PROMPTS


def discover(llm, request: dict, rubric: dict | None = None) -> list[dict]:
    system = (PROMPTS / "discover.md").read_text(encoding="utf-8")
    if rubric and rubric.get("discover_hint"):
        system += f"\n\n## 이번 구매 방식: {rubric.get('label', '')}\n{rubric['discover_hint']}"
    user = (
        "아래 소싱 조건에 맞는 공급처 후보를 찾아주세요.\n"
        f"최소 {request.get('min_candidates') or 20}곳 이상.\n\n"
        f"```yaml\n{yaml.safe_dump(request, allow_unicode=True, sort_keys=False)}```"
    )
    result = llm.ask_json(system, user, use_search=True, max_tokens=64000, stage="discover")
    if isinstance(result, dict):  # 모델이 {"candidates": [...]} 로 감싼 경우
        result = next((v for v in result.values() if isinstance(v, list)), [])
    if not isinstance(result, list):
        return []
    # 형식이 어긋난 항목(문자열, null 등)과 이름 없는 항목은 버림
    return [c for c in result if isinstance(c, dict) and c.get("name")]


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def parse_years(v) -> float:
    """8, 8.0, "8", "8 yrs", "8년" 같은 값을 숫자로. 못 읽으면 0."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return max(float(v), 0)
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else 0


def parse_qty(v) -> int | None:
    """"500 pcs", "1,000개 이하", "2-10 pieces", 50 같은 값에서 첫 수량을 읽음. 못 읽으면 None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group().replace(",", "")) if m else None


def ships_to_korea(v):
    """한국 직배송 여부: True / False / None(모름). 문자열 "true"·"가능" 등도 받음."""
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return True if s in ("true", "yes", "y", "가능") else False if s in ("false", "no", "n", "불가") else None


def prefilter(candidates: list[dict], request: dict) -> list[dict]:
    """LLM 없이 규칙 기반으로 후보를 추려 검증 비용을 아낌."""
    required = {str(c).lower() for c in _as_list(request.get("required_certs"))}
    max_qty = parse_qty(request.get("max_order_qty")) or parse_qty(request.get("moq"))

    def pre_score(c):
        s = 0
        ctype = str(c.get("company_type") or "unknown").lower()
        if ctype == "manufacturer":
            s += 3 if request.get("prefer_manufacturer") else 2
        elif ctype == "trading":
            s += 1
        s += min(parse_years(c.get("years_in_business")), 10) / 2
        certs = {str(x).lower() for x in _as_list(c.get("certifications"))}
        if required:
            s += 3 * len(required & certs) / len(required)
        s += 1 if c.get("website") else 0
        s += 1 if c.get("platform_badges") else 0
        # 원하는 수량 이하로 살 수 있으면 가점, 확실히 초과하면 감점 (모르면 0)
        cand_moq = parse_qty(c.get("moq"))
        if max_qty and cand_moq is not None:
            s += 4 if cand_moq <= max_qty else -4
        # 낱개·소량 구매는 한국 직배송이 되면 배송대행지 요금·시간이 안 듦 → 우선 (국내배송만이면 약간 뒤로, 빼지는 않음)
        if request.get("sourcing_type") in ("retail", "small_batch"):
            ship = ships_to_korea((c.get("trade_terms") or {}).get("ships_to_korea")
                                  if isinstance(c.get("trade_terms"), dict) else None)
            s += 3 if ship is True else -1 if ship is False else 0
        return s

    ranked = sorted(candidates, key=pre_score, reverse=True)
    size = request.get("shortlist_size") or 10
    if request.get("sourcing_type") != "small_batch":
        return ranked[:size]
    # 소량 사입: 소매 마켓 판매자(AliExpress·eBay 상품 페이지)는 최대 RETAIL_CAP 곳 — 소매가는 도매 단가보다 비싸서
    # 소매 판매자로만 채우면 단가 상한을 맞추는 곳이 안 남음. 도매 후보가 모자라면 그때 소매 판매자로 채움
    retail = [c for c in ranked if is_retail_listing(c)]
    keep = [c for c in ranked if not is_retail_listing(c)] + retail[:RETAIL_CAP]
    keep = sorted(keep, key=ranked.index)[:size]
    extra = [c for c in retail[RETAIL_CAP:] if c not in keep]
    return keep + extra[: size - len(keep)]


RETAIL_CAP = 2
_RETAIL_HOSTS = ("aliexpress.", "ebay.")


def is_retail_listing(c) -> bool:
    """소매 마켓(AliExpress·eBay) 상품 페이지만 있는 후보인지 (도매 페이지가 하나라도 있으면 도매로 봄)."""
    urls = [str(u).lower() for u in _as_list(c.get("urls")) if u]
    return bool(urls) and all(any(h in u for h in _RETAIL_HOSTS) for u in urls)
