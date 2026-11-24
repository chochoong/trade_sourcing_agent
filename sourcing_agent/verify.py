"""④ 신뢰도 검증: 후보 1곳씩 교차 확인 + 채점."""
import json

import anthropic
import yaml

from .errors import friendly
from .discover import ships_to_korea
from .manual import read_manual
from .normalize import order_urls, parse_usd_price
from .paths import PROMPTS
from .scoring import compute, rubric_text
from .packaging import normalize_packaging
from .specs import normalize_specs

# 업체를 바꿔도 똑같이 실패할 오류 → 전체 실행을 즉시 중단
# (API 키 오류, 권한 없음, 잘못된 모델명/툴 버전 등)
FATAL_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.BadRequestError,
    anthropic.NotFoundError,
)


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


CONTACT_KEYS = ("contact_person", "email", "phone", "messenger", "address")
TRADE_TERM_KEYS = ("lead_time", "payment_terms", "sample", "export_markets", "ships_to_korea", "korea_shipping")


def _merge(keys, found, verified) -> dict:
    """검색 단계 값 위에 검증 단계에서 확인한 값(null 아닌 것)을 덮어씀."""
    found = found if isinstance(found, dict) else {}
    verified = verified if isinstance(verified, dict) else {}
    merged = {k: verified.get(k) if verified.get(k) not in (None, "", []) else found.get(k) for k in keys}
    # export_markets 는 항상 리스트로 (다음 단계 에이전트가 형식을 믿고 읽을 수 있게)
    if "export_markets" in merged and merged["export_markets"] is not None:
        merged["export_markets"] = [str(x) for x in _as_list(merged["export_markets"])]
    if "ships_to_korea" in merged:   # true / false / null 만 (문자열 "true" 도 받음, 그 밖은 모름)
        merged["ships_to_korea"] = ships_to_korea(merged["ships_to_korea"])
    return merged


def _failed(reason: str) -> dict:
    return {"scores": {}, "risk_flags": [f"검증 실패: {reason}"], "unverified": ["전체"],
            "summary": "자동 검증에 실패했습니다. 수동 확인이 필요합니다."}


def verify_one(llm, candidate: dict, request: dict, rubric: dict) -> dict:
    system = (PROMPTS / "verify.md").read_text(encoding="utf-8")
    system += "\n\n## 점수표\n" + rubric_text(rubric)

    manual = read_manual(candidate)
    user = (
        "## 소싱 조건\n"
        f"```yaml\n{yaml.safe_dump(request, allow_unicode=True, sort_keys=False)}```\n\n"
        "## 검증할 후보\n"
        f"```json\n{json.dumps(candidate, ensure_ascii=False, indent=2)}\n```\n\n"
        "## 사람이 확인한 추가 정보\n"
        + (manual if manual else "(없음)")
    )
    try:
        v = llm.ask_json(system, user, use_search=True, stage="verify")
    except FATAL_ERRORS:
        raise
    except Exception as e:  # 응답 형식 오류, 일시적 장애 등은 한 업체만 실패 처리하고 계속
        v = _failed(friendly(e))

    if not isinstance(v, dict):
        v = _failed(f"응답 형식이 객체가 아님 ({type(v).__name__})")
    if not isinstance(v.get("scores"), dict):
        v["scores"] = {}

    evidence = []
    for e in _as_list(v.get("evidence")):
        if isinstance(e, dict):
            evidence.append({"claim": e.get("claim"), "url": e.get("url")})
        elif e:
            evidence.append({"claim": str(e), "url": None})

    # 링크: 상품 상세 페이지 → 스토어·업체 고유 페이지 → (참고용) 목록 페이지 순
    urls = [str(u) for u in _as_list(candidate.get("urls")) if u]
    store = v.get("store_url") if isinstance(v.get("store_url"), str) else None
    urls = order_urls(urls + ([store] if store else []))
    price_range = v.get("price_range") or candidate.get("price_range")

    result = compute(v, rubric)
    return {
        **candidate,
        **result,
        "urls": urls,
        "price_range": price_range,
        "unit_price_usd": parse_usd_price(price_range),   # 원가 계산용 숫자 (범위의 상한, USD)
        "contacts": _merge(CONTACT_KEYS, candidate.get("contacts"), v.get("contacts")),
        "trade_terms": _merge(TRADE_TERM_KEYS, candidate.get("trade_terms"), v.get("trade_terms")),
        "risk_flags": [str(x) for x in _as_list(v.get("risk_flags"))],
        "unverified": [str(x) for x in _as_list(v.get("unverified"))],
        "evidence": evidence,
        "summary": str(v.get("summary") or ""),
        "first_questions": [str(x) for x in _as_list(v.get("first_questions"))],
        "manual_info_used": bool(manual),
        "specs": normalize_specs(v.get("specs")),   # 제품 사양 (3번 통관·규제 체크용). 모르면 None
        "packaging": normalize_packaging(v.get("packaging")),   # 포장·무게 (4번 운임 추정용). 모르면 None
    }
