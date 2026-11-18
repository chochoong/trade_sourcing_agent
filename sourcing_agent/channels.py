"""구매 사이트(채널) 추천: 이 품목을 어느 사이트에서 사는 게 좋은지 점수와 검색 링크로 보여 줌.

점수 = 항목별 평가(0~5) ÷ 5 × 가중치 (config/channels.yaml).
  품목별 항목(소량 구매·가격·선택지)은 웹 검색으로 매번 평가하고,
  사이트 고유 항목(구매자 보호·한국 배송·거래 편의·서류)은 설정 파일의 고정값을 씀.
검색 링크는 AI가 아니라 코드가 만들어서 항상 열리는 주소가 됨.
"""
import json
import re
from urllib.parse import quote, quote_plus

import yaml

from .errors import friendly
from .paths import CONFIG, PROMPTS

CHANNELS_FILE = CONFIG / "channels.yaml"


def load_channels() -> dict:
    cfg = yaml.safe_load(CHANNELS_FILE.read_text(encoding="utf-8"))
    for t, w in cfg["weights"].items():
        total = sum(w.values())
        if total != 100:
            raise ValueError(f"config/channels.yaml weights.{t} 합계가 100이 아닙니다 ({total})")
    return cfg


COUNTRY_ALIASES = {"china": "China", "중국": "China", "cn": "China",
                   "japan": "Japan", "일본": "Japan", "jp": "Japan"}


def request_country(request: dict) -> str:
    """조사 조건의 첫 선호 국가 (없으면 China)."""
    c = request.get("preferred_countries") or ["China"]
    c = c[0] if isinstance(c, list) and c else c
    return COUNTRY_ALIASES.get(str(c or "").strip().lower(), str(c or "China"))


def sites_for(cfg: dict, sourcing_type: str, country: str = "China") -> dict:
    return {k: s for k, s in cfg["sites"].items()
            if sourcing_type in (s.get("suits") or []) and country in (s.get("countries") or ["China"])}


def _short_keyword(text: str) -> str:
    """'mechanical keyboard hot swappable ... ready to ship' 같은 긴 검색어를 앞쪽 몇 단어로."""
    words = re.sub(r"[()\[\],/]", " ", str(text or "")).split()
    return " ".join(words[:5])


def search_url(template: str, en: str, zh: str | None, ja: str | None = None) -> str:
    zh = zh or en
    ja = ja or en
    try:
        zh_gbk = quote(zh.encode("gbk"))
    except UnicodeEncodeError:
        zh_gbk = quote(zh.encode("gbk", errors="ignore"))
    return template.format(
        en=quote_plus(en),
        en_dash=quote("-".join(en.lower().split()), safe="-"),
        zh=quote(zh),
        zh_gbk=zh_gbk,
        ja=quote(ja),
    )


def _rating(v) -> float:
    try:
        return min(max(float(v), 0.0), 5.0)
    except (TypeError, ValueError):
        return 0.0


def score_site(site: dict, assessed: dict, weights: dict) -> tuple[float, dict]:
    """(총점, 항목별 점수). 품목별 항목은 assessed(LLM 평가), 고유 항목은 site['base']."""
    base = site.get("base") or {}
    parts = {}
    for key, w in weights.items():
        r = _rating(assessed.get(key)) if key in ("small_qty", "price", "selection") else _rating(base.get(key))
        parts[key] = round(r / 5 * w, 1)
    return round(sum(parts.values()), 1), parts


def build_channels(cfg: dict, request: dict, assessment: dict | None) -> list[dict]:
    """평가 결과(없어도 됨) + 설정 → 점수순 사이트 목록."""
    stype = request.get("sourcing_type") or "oem"
    weights = cfg["weights"].get(stype) or cfg["weights"]["small_batch"]
    assessment = assessment if isinstance(assessment, dict) else {}
    found = {str(s.get("key")): s for s in assessment.get("sites") or [] if isinstance(s, dict)}
    en = _short_keyword(assessment.get("keyword_en") or request.get("product_en") or request.get("product"))
    zh = assessment.get("keyword_zh") or request.get("product_zh")
    ja = assessment.get("keyword_ja") or request.get("product_ja")

    out = []
    for key, site in sites_for(cfg, stype, request_country(request)).items():
        a = found.get(key, {})
        total, parts = score_site(site, a, weights)
        evidence = [{"claim": e.get("claim"), "url": e.get("url")} for e in a.get("evidence") or [] if isinstance(e, dict)]
        out.append({
            "key": key, "site": site.get("label", key),
            "search_url": search_url(site["search"], en, zh, ja),
            "total": total, "scores": parts,
            "typical_price": a.get("typical_price"), "min_qty": a.get("min_qty"),
            "summary": a.get("summary") or ("평가 못 함 (사이트 고유 점수만 반영)" if not a else ""),
            "site_note": site.get("note"),
            "evidence": evidence,
            "assessed": bool(a),
        })
    out.sort(key=lambda c: c["total"], reverse=True)
    return [{"rank": i, **c} for i, c in enumerate(out, 1)]


def assess_channels(llm, request: dict, log=None) -> list[dict]:
    """웹 검색으로 사이트별 품목 평가 → 점수순 목록. 평가가 실패해도 고유 점수·링크는 돌려줌."""
    cfg = load_channels()
    stype = request.get("sourcing_type") or "oem"
    sites = sites_for(cfg, stype, request_country(request))
    if not sites:
        return []
    system = (PROMPTS / "channels.md").read_text(encoding="utf-8")
    user = (
        "## 소싱 조건\n"
        f"```yaml\n{yaml.safe_dump(request, allow_unicode=True, sort_keys=False)}```\n\n"
        "## 평가할 사이트\n"
        f"```json\n{json.dumps([{'key': k, 'site': s.get('label', k)} for k, s in sites.items()], ensure_ascii=False)}\n```"
    )
    assessment = None
    try:
        assessment = llm.ask_json(system, user, use_search=True, stage="channels")
    except Exception as e:  # 사이트 평가는 부가 정보 — 실패해도 공급처 결과는 그대로 냄
        if log:
            log(f"      구매 사이트 평가 실패 (고유 점수·링크만 표시): {friendly(e)}")
    return build_channels(cfg, request, assessment)
