"""경쟁 제품 대비 평가 (competitive) — 검증 단계에서 이 업체 상품을 같은 품목의 경쟁 제품과 사양·리뷰로 비교.

results.json suppliers[].competitive:
  {"competitors": [{"name": "로지텍 G304", "url": "...", "price": "29,000원"}],
   "attributes": [{"name": "폴링레이트", "ours": "1000Hz", "competitor": "8000Hz (A사)", "verdict": "열위"}],
   "review": {"rating": 4.6, "count": 1200, "complaints": ["더블클릭", "배터리 수명"]},
   "summary": "경쟁 대비 …"}
공용 DB 에는 evidence_json 에 {"claim", "url", "type": "competitive"} 항목으로 덧붙임 (기존 {claim, url} 형식 유지).
확인한 값만. 비교할 게 없으면 None.
"""
import re

VERDICTS = {"우위": "우위", "advantage": "우위", "better": "우위", "열위": "열위", "disadvantage": "열위",
            "worse": "열위", "동등": "동등", "same": "동등", "equal": "동등", "similar": "동등"}
MAX_ATTRS, MAX_COMPETITORS, MAX_COMPLAINTS = 10, 5, 8


def _s(v, n=80):
    s = str(v).strip() if v not in (None, "", []) else ""
    return "" if s.lower() in ("null", "none", "모름", "unknown") else s[:n]


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v if v > 0 else None
    m = re.search(r"\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    if not m:
        return None
    x = float(m.group(0))
    return (int(x) if x.is_integer() else x) if x > 0 else None


def normalize_competitive(v):
    if not isinstance(v, dict):
        return None
    out = {}
    comps = [{"name": _s(c.get("name")), "url": _s(c.get("url"), 300) or None, "price": _s(c.get("price"), 40) or None}
             for c in (v.get("competitors") or []) if isinstance(c, dict) and _s(c.get("name"))]
    if comps:
        out["competitors"] = comps[:MAX_COMPETITORS]
    attrs = []
    for a in v.get("attributes") or []:
        if not isinstance(a, dict) or not _s(a.get("name")):
            continue
        ours, theirs = _s(a.get("ours")), _s(a.get("competitor"))
        if not ours and not theirs:
            continue
        row = {"name": _s(a.get("name"), 30), "ours": ours or None, "competitor": theirs or None}
        verdict = VERDICTS.get(_s(a.get("verdict")).lower())
        if verdict:
            row["verdict"] = verdict
        attrs.append(row)
    if attrs:
        out["attributes"] = attrs[:MAX_ATTRS]
    r = v.get("review") if isinstance(v.get("review"), dict) else {}
    rating, count = _num(r.get("rating")), _num(r.get("count"))
    complaints = [_s(x, 40) for x in (r.get("complaints") or []) if _s(x)] if isinstance(r.get("complaints"), list) else []
    review = {k: val for k, val in (("rating", rating if rating and rating <= 5 else None), ("count", count and int(count)),
                                    ("complaints", complaints[:MAX_COMPLAINTS] or None)) if val}
    if review:
        out["review"] = review
    summary = _s(v.get("summary"), 300)
    if summary:
        out["summary"] = summary
    return out or None


def verdict_counts(c):
    attrs = (c or {}).get("attributes") or []
    return {k: sum(a.get("verdict") == k for a in attrs) for k in ("우위", "열위", "동등")}


def competitive_evidence(c, product_url=None):
    """evidence_json 에 덧붙일 항목들 ({claim, url, type: "competitive"})."""
    if not c:
        return []
    first = next((x for x in c.get("competitors") or [] if x.get("url")), None)
    ev = []
    for a in c.get("attributes") or []:
        tag = f"[{a['verdict']}] " if a.get("verdict") else ""
        ev.append({"claim": f"경쟁 비교 {tag}{a['name']}: 이 제품 {a.get('ours') or '미확인'} vs 경쟁 {a.get('competitor') or '미확인'}",
                   "url": product_url, "type": "competitive"})
    r = c.get("review") or {}
    if r:
        parts = [f"평점 {r['rating']:g}" if r.get("rating") else "", f"{r['count']:,}건" if r.get("count") else "",
                 "불만: " + ", ".join(r["complaints"]) if r.get("complaints") else ""]
        ev.append({"claim": "리뷰 " + " · ".join(p for p in parts if p), "url": product_url, "type": "competitive"})
    for x in c.get("competitors") or []:
        ev.append({"claim": f"비교 경쟁 제품: {x['name']}" + (f" ({x['price']})" if x.get("price") else ""),
                   "url": x.get("url"), "type": "competitive"})
    if c.get("summary"):
        n = verdict_counts(c)
        ev.append({"claim": f"경쟁 대비 요약 (우위 {n['우위']} · 열위 {n['열위']}): {c['summary']}",
                   "url": (first or {}).get("url"), "type": "competitive"})
    return ev


def competitive_text(c):
    """리포트 한 줄: '우위 2 · 열위 1 — 요약 · 리뷰 4.6 (불만: 배터리)'."""
    if not c:
        return ""
    n = verdict_counts(c)
    head = " · ".join(f"{k} {v}" for k, v in n.items() if v)
    r = c.get("review") or {}
    review = (f"리뷰 {r['rating']:g}" if r.get("rating") else "") + (
        f" (불만: {', '.join(r['complaints'][:3])})" if r.get("complaints") else "")
    return " — ".join(x for x in [head, c.get("summary") or "", review.strip()] if x)
