"""제품 품질·리뷰 평가 (quality) — 판매자 평판과 별개로 '제품 자체'가 좋은지. 4번 보고서·대시보드가 마진 옆에 표시 (계산엔 안 씀).

검증이 끝난 뒤 **모델별로 한 번** 조사 (같은 모델을 여러 판매자가 팔아도 리뷰는 같음 → 비용 절약).
모델명은 검증 단계가 상품 페이지에서 확인한 suppliers[].model.

suppliers[].quality (공용 DB: product_suppliers.quality_json — trade_hub 에 칸이 있을 때):
  {"model": "AULA F75", "review_sources": 6, "review_count_total": 3400,
   "pros": [{"point": "타건감 좋음", "url": "..."}],
   "cons": [{"point": "2.4G 연결 끊김", "url": "..."}],
   "spec_doubts": [{"spec": "배터리", "claimed": "4000mAh", "observed": "실사용 2주", "status": "confirmed|exaggerated|unverified", "url": "..."}],
   "competitors": [{"name": "…", "url": "…", "price": "…"}],
   "attributes": [{"name": "폴링레이트", "ours": "1000Hz", "competitor": "8000Hz (A사)", "verdict": "우위|열위|동등"}],
   "vs_competitors": "경쟁 대비 한두 줄", "verdict": "good|ok|weak|unknown", "sample_needed": true,
   "note": "리뷰 부족 — 샘플 확인 필요", "evidence": [{"claim", "url"}], "checked_at": "2026-11-25"}
evidence_json 에도 {"claim", "url", "type": "quality"} 항목으로 덧붙임 (요약·불만·사양 과장만).
"""
import json
import re
from datetime import date

from .errors import friendly
from .paths import PROMPTS

VERDICTS = ("good", "ok", "weak", "unknown")
ATTR_VERDICTS = {"우위": "우위", "advantage": "우위", "better": "우위", "열위": "열위", "disadvantage": "열위",
                 "worse": "열위", "동등": "동등", "same": "동등", "equal": "동등", "similar": "동등"}
SPEC_STATUS = ("confirmed", "exaggerated", "unverified")
UNKNOWN_NOTE = "리뷰가 부족해 판단 불가 — 샘플 확인 필요"


def _s(v, n=120):
    s = str(v).strip() if v not in (None, "", []) else ""
    return "" if s.lower() in ("null", "none", "모름", "unknown", "n/a") else s[:n]


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v >= 0 else None
    m = re.search(r"\d+", str(v or "").replace(",", ""))
    return int(m.group(0)) if m else None


def _url(v):
    s = _s(v, 500)
    return s if s.lower().startswith(("http://", "https://")) else None


def _points(v, n=10):
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict) and _s(x.get("point")):
            out.append({"point": _s(x["point"], 80), "url": _url(x.get("url"))})
        elif isinstance(x, str) and _s(x):
            out.append({"point": _s(x, 80), "url": None})
    return out[:n]


def model_key(model):
    """'AULA F75 ' / 'aula-f75' → 'aulaf75' (같은 모델 묶기). 모델명이 아니면 None."""
    s = re.sub(r"[^0-9a-z가-힣]", "", str(model or "").lower())
    return s if len(s) >= 3 and s not in ("null", "none", "unknown", "모름", "무브랜드", "nobrand", "oem") else None


def normalize_quality(v, model=None):
    if not isinstance(v, dict):
        return None
    out = {"model": _s(v.get("model"), 80) or _s(model, 80) or None}
    for k in ("review_sources", "review_count_total"):
        n = _int(v.get(k))
        if n is not None:
            out[k] = n
    for k in ("pros", "cons"):
        pts = _points(v.get(k))
        if pts:
            out[k] = pts
    doubts = []
    for d in v.get("spec_doubts") or []:
        if isinstance(d, dict) and _s(d.get("spec")):
            st = _s(d.get("status")).lower()
            doubts.append({"spec": _s(d["spec"], 30), "claimed": _s(d.get("claimed"), 60) or None,
                           "observed": _s(d.get("observed"), 80) or None,
                           "status": st if st in SPEC_STATUS else "unverified", "url": _url(d.get("url"))})
    if doubts:
        out["spec_doubts"] = doubts[:10]
    comps = [{"name": _s(c.get("name"), 60), "url": _url(c.get("url")), "price": _s(c.get("price"), 40) or None}
             for c in v.get("competitors") or [] if isinstance(c, dict) and _s(c.get("name"))]
    if comps:
        out["competitors"] = comps[:5]
    attrs = []
    for a in v.get("attributes") or []:
        if not isinstance(a, dict) or not _s(a.get("name")):
            continue
        ours, theirs = _s(a.get("ours"), 60), _s(a.get("competitor"), 60)
        if ours or theirs:
            row = {"name": _s(a["name"], 30), "ours": ours or None, "competitor": theirs or None}
            if ATTR_VERDICTS.get(_s(a.get("verdict")).lower()):
                row["verdict"] = ATTR_VERDICTS[_s(a.get("verdict")).lower()]
            attrs.append(row)
    if attrs:
        out["attributes"] = attrs[:10]
    vs = _s(v.get("vs_competitors"), 300)
    if vs:
        out["vs_competitors"] = vs
    verdict = _s(v.get("verdict")).lower()
    out["verdict"] = verdict if verdict in VERDICTS else "unknown"
    # 리뷰 출처가 없으면 판단을 믿을 수 없음 → unknown
    if not out.get("review_sources") and not out.get("pros") and not out.get("cons"):
        out["verdict"] = "unknown"
    out["sample_needed"] = out["verdict"] in ("unknown", "weak") or bool(v.get("sample_needed") is True)
    note = _s(v.get("note"), 200)
    if out["verdict"] == "unknown" and not note:
        note = UNKNOWN_NOTE
    if note:
        out["note"] = note
    ev = [{"claim": _s(e.get("claim"), 200), "url": _url(e.get("url"))} for e in v.get("evidence") or []
          if isinstance(e, dict) and _s(e.get("claim"))]
    if ev:
        out["evidence"] = ev[:15]
    out["checked_at"] = date.today().isoformat()
    return out


def unknown_quality(model, note):
    return {"model": model or None, "verdict": "unknown", "sample_needed": True, "note": note,
            "checked_at": date.today().isoformat()}


VERDICT_KO = {"good": "좋음", "ok": "보통", "weak": "약함", "unknown": "판단 불가"}


def quality_text(q):
    """리포트 한 줄: '좋음 · 리뷰 6곳 3,400건 · 불만: 연결 끊김, 채터링 · 과장 의심: 배터리 — 경쟁 대비 …'."""
    if not q:
        return ""
    parts = [VERDICT_KO.get(q.get("verdict"), "판단 불가")]
    if q.get("review_sources"):
        parts.append(f"리뷰 {q['review_sources']}곳" + (f" {q['review_count_total']:,}건" if q.get("review_count_total") else ""))
    if q.get("cons"):
        parts.append("불만: " + ", ".join(c["point"] for c in q["cons"][:3]))
    bad = [d["spec"] for d in q.get("spec_doubts") or [] if d["status"] == "exaggerated"]
    if bad:
        parts.append("과장 의심: " + ", ".join(bad))
    if q.get("sample_needed"):
        parts.append("샘플 확인 필요")
    text = " · ".join(parts)
    return f"{text} — {q['vs_competitors']}" if q.get("vs_competitors") else text


def quality_evidence(q):
    """evidence_json 에 덧붙일 요약 항목 ({claim, url, type: "quality"})."""
    if not q:
        return []
    ev = [{"claim": f"제품 품질 {VERDICT_KO.get(q['verdict'])} ({q.get('model') or '모델 미확인'}): {quality_text(q)}",
           "url": None, "type": "quality"}]
    ev += [{"claim": f"제품 불만: {c['point']}", "url": c["url"], "type": "quality"} for c in q.get("cons") or []]
    ev += [{"claim": f"사양 과장 의심: {d['spec']} 표기 {d.get('claimed') or '?'} / 리뷰 {d.get('observed') or '?'}",
            "url": d["url"], "type": "quality"} for d in q.get("spec_doubts") or [] if d["status"] == "exaggerated"]
    return ev


def assess_model(llm, model, product_name, results, request):
    system = (PROMPTS / "quality.md").read_text(encoding="utf-8")
    pages = [{"supplier": r.get("name"), "urls": (r.get("urls") or [])[:2], "price_range": r.get("price_range"),
              "specs": (r.get("specs") or {}).get("flags"), "materials": (r.get("specs") or {}).get("materials")}
             for r in results]
    user = (f"## 품목\n{product_name}\n\n## 모델\n{model}\n\n"
            f"## 이 모델을 파는 공급처 상품 페이지 (판매 페이지 사양)\n```json\n{json.dumps(pages, ensure_ascii=False, indent=1)}\n```\n\n"
            f"## 참고 상품 (시장조사)\n{json.dumps(request.get('reference_urls') or [], ensure_ascii=False)}\n"
            f"## 국내 판매가\n{request.get('target_price') or '모름'}")
    return llm.ask_json(system, user, use_search=True, stage="quality")


def assess_quality(llm, results, request, log=print, max_models=5):
    """검증 결과에 quality 를 붙임 (모델별 1회, 점수 높은 업체의 모델부터 max_models 개). 반환: 조사한 모델 수."""
    groups = {}
    for r in sorted(results, key=lambda r: r.get("total") or 0, reverse=True):
        key = model_key(r.get("model"))
        if key:
            groups.setdefault(key, []).append(r)
    done = 0
    for key, rs in groups.items():
        model = rs[0].get("model")
        if done >= max_models:
            q = unknown_quality(model, f"품질 조사 대상 수({max_models}개 모델) 초과로 건너뜀 — 샘플 확인 필요")
        else:
            log(f"[+] 제품 품질·리뷰 조사 ({done + 1}): {model}")
            try:
                q = normalize_quality(assess_model(llm, model, request.get("product"), rs, request), model)
            except Exception as e:   # 품질 조사가 실패해도 검증 결과는 그대로
                q = unknown_quality(model, f"품질 조사 실패: {friendly(e)} — 샘플 확인 필요")
            done += 1
        for r in rs:
            r["quality"] = q
    for r in results:
        if "quality" not in r:
            r["quality"] = unknown_quality(None, "상품 페이지에서 모델명을 확인하지 못함 (무브랜드·OEM) — 샘플 확인 필요")
        r["evidence"] = (r.get("evidence") or []) + quality_evidence(r["quality"])
    return done
