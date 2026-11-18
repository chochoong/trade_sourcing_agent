"""공용 DB(trade_hub) 연동.

1번 에이전트가 찾고 사람이 `trade_hub select`로 넘긴 품목(stage=sourcing)을 DB에서 읽어
공급처를 찾고, 결과를 suppliers·product_suppliers에 기록한다.

  python -m sourcing_agent from-db              # 아직 조사하지 않은 sourcing 품목 전부
  python -m sourcing_agent from-db 16 17        # 지정한 품목만 (이미 조사했어도 다시)

이 에이전트가 쓰는 곳: suppliers, product_suppliers, tasks(검토 요청), product_events(조사 기록).
단계(stage)는 바꾸지 않는다 — 공급처를 골라 협상으로 넘기는 건 사람/3번 에이전트의 일.
"""
import json
from pathlib import Path

from trade_hub import db

from .agent import log_stderr, run_discover, run_verify
from .discover import parse_qty, parse_years
from .llm import LLM
from .normalize import best_url, parse_usd_price
from .scoring import load_rubric

AGENT = "sourcing"
TOP_PICKS = 3
TRUST = {"A": "상", "B": "중", "C": "하", "D": "하"}

# 이 에이전트가 이미 조사한 품목은 공급처를 못 찾았어도 자동으로 다시 돌리지 않는다 (비용 반복 방지).
# 슈퍼바이저 supervisor.json의 sourcing.when_sql도 같은 조건을 쓴다.
PENDING_SQL = """
SELECT * FROM products p
WHERE p.stage = 'sourcing'
  AND NOT EXISTS (SELECT 1 FROM product_suppliers ps WHERE ps.product_id = p.id)
  AND NOT EXISTS (SELECT 1 FROM product_events e JOIN agent_runs r ON r.id = e.run_id
                  WHERE e.product_id = p.id AND r.agent = 'sourcing')
ORDER BY p.id"""


def pending_products(conn, ids=None):
    if not ids:
        return conn.execute(PENDING_SQL).fetchall()
    rows = conn.execute(f"SELECT * FROM products WHERE id IN ({', '.join('?' * len(ids))}) ORDER BY id",
                        list(ids)).fetchall()
    found = {r["id"] for r in rows}
    for i in ids:
        if i not in found:
            log_stderr(f"  · 건너뜀: 품목 번호 {i}가 없습니다")
    ok = []
    for r in rows:
        if r["stage"] != "sourcing":
            log_stderr(f"  · 건너뜀: [{r['id']}] {r['name']} — 단계가 {r['stage']} "
                       f"(먼저 `trade_hub select {r['id']}`)")
        else:
            ok.append(r)
    return ok


def build_request(conn, p, sourcing_type="small_batch", max_order_qty=None):
    """DB 품목 → 소싱 조건 dict (YAML 요청 파일과 같은 형식)."""
    s = conn.execute("SELECT est_fob_usd FROM product_scores WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                     (p["id"],)).fetchone()
    fob = s["est_fob_usd"] if s else None
    price = []
    if fob:
        price.append(f"FOB 약 USD {fob:g} 내외 (시장조사 단계 웹 시세)")
    if p["est_retail_krw"]:
        price.append(f"국내 판매가 약 {p['est_retail_krw']:,}원 — 마진이 남는 단가일 것")
    notes = [f"- {label}: {p[k]}" for k, label in
             (("reason", "선정 이유"), ("regulations", "예상 인증·규제 (판매자가 시험성적서를 줄 수 있는지 확인)"),
              ("risk", "위험 요소"), ("notes", "메모")) if p[k]]
    req = {
        "mode": "supplier",
        "sourcing_type": sourcing_type,
        "product": p["name"],
        "product_en": p["search_keyword_en"] or p["name"],
        "hs_code": p["hs_code"],
        "target_price": " / ".join(price) or None,
        "required_certs": [],          # 인증 확정은 5번(통관·규제) 단계 — 여기서는 notes로만 전달
        "preferred_countries": ["China"],
        "prefer_manufacturer": sourcing_type == "oem",
        "notes": "\n".join(notes) or None,
        "min_candidates": 15,
        "shortlist_size": 5,
        "trade_hub_product_id": p["id"],
    }
    if max_order_qty:
        req["max_order_qty"] = max_order_qty
    return {k: v for k, v in req.items() if v is not None}


# DB 설계(schema.sql)의 업체 유형 표기: 제조사 / 무역상 / 미확인
COMPANY_TYPE_KO = {"manufacturer": "제조사", "trading": "무역상", "trading company": "무역상",
                   "제조사": "제조사", "무역상": "무역상"}


def _company_type(conn, company, country, value):
    """영문 유형을 한글로. 모르는 값이면 새 업체는 '미확인', 기존 업체는 None(이전 값 유지)."""
    ko = COMPANY_TYPE_KO.get(str(value or "").strip().lower())
    if ko:
        return ko
    exists = conn.execute("SELECT 1 FROM suppliers WHERE company = ? AND country IS ?",
                          (company, country)).fetchone()
    return None if exists else "미확인"


def _text(v):
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if x)
    return str(v) if v not in (None, "") else None


def _list(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


def save_results(conn, run_id, product_id, suppliers, report_path, purchase_mode=None, max_order_qty=None):
    """results.json의 suppliers → suppliers + product_suppliers (+ 사람 검토 할 일).

    purchase_mode·max_order_qty: 조사 때 구매 방식(retail / small_batch / oem)과 첫 구매 최대 수량
    — 대시보드가 MOQ 배지(소량 OK / 초과 / 미확인)의 기준으로 씀.
    """
    with conn:
        for i, r in enumerate(suppliers):
            c = r.get("contacts") if isinstance(r.get("contacts"), dict) else {}
            other = " / ".join(f"{k} {c[k]}" for k in ("phone", "messenger") if c.get(k)) or None
            company, country = r.get("name") or "이름 미확인", _text(r.get("country"))
            sid = db.upsert_supplier(
                conn, company, country,
                type=_company_type(conn, company, country, r.get("company_type")),
                years_in_business=parse_years(r.get("years_in_business")) or None,
                certifications=_text(r.get("certifications")),
                platform_badge=_text(r.get("platform_badges")),
                url=best_url(r),          # 검색·목록 페이지는 제외 (없으면 기존 값 유지)
                contact_name=_text(c.get("contact_person")), contact_email=_text(c.get("email")),
                contact_other=other,
                trust=TRUST.get(r.get("grade")),
                trust_reason=(f"{r.get('total')}점 ({r.get('grade')}) — {r.get('summary') or ''}").strip(" —"),
                red_flags=_text(r.get("risk_flags")))
            link = {"is_top_pick": int(i < TOP_PICKS), "moq": _text(r.get("moq")),
                    "price_note": _text(r.get("price_range")),
                    # v4: 이 품목 기준 평가 결과 (3번 협상은 first_questions·trade_terms 를 씀)
                    "purchase_mode": purchase_mode, "max_order_qty": max_order_qty,
                    "score": r.get("total"), "grade": r.get("grade") if r.get("grade") in TRUST else None,
                    "score_detail_json": r.get("scores") if isinstance(r.get("scores"), dict) else None,
                    "trade_terms_json": r.get("trade_terms") if isinstance(r.get("trade_terms"), dict) else None,
                    "first_questions_json": _list(r.get("first_questions")),
                    "unverified_json": _list(r.get("unverified")),
                    "evidence_json": _list(r.get("evidence"))}
            # 원가 계산(6번)은 unit_price_usd 가 있는 업체만 처리함 → 문장형 단가에서 USD 숫자를 뽑아 넣음
            price = r.get("unit_price_usd") or parse_usd_price(r.get("price_range"))
            if price:
                link["unit_price_usd"] = price
            db.link_supplier(conn, product_id, sid, run_id, **link)
        name = conn.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()["name"]
        n = len(suppliers)
        conn.execute("INSERT INTO product_events (product_id, run_id, from_stage, to_stage, note)"
                     " VALUES (?, ?, 'sourcing', 'sourcing', ?)", (product_id, run_id, f"공급처 조사: {n}곳"))
        title = (f"[{name}] 공급처 {n}곳 검토 — 연락할 곳 고르기" if n
                 else f"[{name}] 공급처를 찾지 못함 — 조건을 바꿔 재조사할지 중단할지 결정")
        db.add_task(conn, "decision", title, product_id=product_id, run_id=run_id, detail=report_path,
                    priority=2 if n else 1)
    return n


# 구매 방식별 첫 구매 최대 수량 기본값 (--max-qty 로 바꿀 수 있음). oem 은 수량 제한 없음.
DEFAULT_MAX_QTY = {"retail": 10, "small_batch": 50}


def export_to_buyagent(run_dir, log=log_stderr):
    """retail(구매대행) 조사 결과를 buyagent 카탈로그에 추가·갱신. 실패해도 조사 결과는 그대로 둠."""
    from .buyagent_export import export
    try:
        r = export(run_dir, write=True)
    except Exception as e:
        log(f"  ⚠ buyagent 내보내기 생략: {e}")
        return {"error": str(e)}
    for w in r["warnings"]:
        log(f"  ⚠ {w}")
    n = len(r["added"]) + len(r["updated"]) + len(r["unchanged"]) + len(r["kept_manual"])
    if not n:
        log(f"  ⚠ buyagent 내보내기 생략: 낱개로 살 수 있고 상품 페이지·USD 단가가 있는 판매처가 없음"
            f" ({len(r['skipped'])}곳 제외)")
    else:
        log(f"  🛒 buyagent: 품목 {r['product_id']}({r['category']}) · {r['summary']} · 제외 {len(r['skipped'])}곳")
    return {"product_id": r["product_id"], "category": r["category"], "summary": r["summary"],
            "skipped": len(r["skipped"])}


def run_from_db(ids=None, sourcing_type="small_batch", max_order_qty=None, dry_run=False, log=log_stderr,
                to_buyagent=True):
    """to_buyagent: retail 조사면 끝난 뒤 결과를 구매대행 모니터(buyagent) 카탈로그에 자동 추가."""
    load_rubric(sourcing_type)  # 없는 구매 방식이면 DB를 열기 전에 바로 오류
    if max_order_qty is None:
        max_order_qty = DEFAULT_MAX_QTY.get(sourcing_type)
    conn = db.connect()
    log(f"공용 DB: {db.default_path()}")
    products = pending_products(conn, ids)
    if not products:
        log("처리할 품목이 없습니다 (`trade_hub select <번호>`로 넘긴 품목 중 아직 조사하지 않은 것).")
        return {"processed": []}
    requests = [(p, build_request(conn, p, sourcing_type, max_order_qty)) for p in products]
    if dry_run:
        return {"dry_run": [r for _, r in requests]}

    done = []
    args = {"ids": ids, "sourcing_type": sourcing_type, "max_order_qty": max_order_qty}
    with db.agent_run(conn, AGENT, provider="anthropic", model=LLM().model, args=args) as run:
        for p, req in requests:
            log(f"\n▶ [{p['id']}] {p['name']} 공급처 조사")
            d = run_discover(req, log=log)
            v = run_verify(d["run_dir"], log=log)
            payload = json.loads(Path(v["files"]["json"]).read_text(encoding="utf-8"))
            report = v["files"].get("html") or v["files"]["markdown"]
            n = save_results(conn, run.id, p["id"], payload["suppliers"], report,
                             purchase_mode=payload.get("sourcing_type") or sourcing_type,
                             max_order_qty=parse_qty((payload.get("request") or {}).get("max_order_qty")))
            # 구매 사이트 순위: results.json 의 전체 channels (run_verify 반환값의 상위 3개 요약은 단가·장단점이 없음)
            with conn:
                nc = db.save_channels(conn, p["id"], run.id, payload.get("channels"),
                                      purchase_mode=payload.get("sourcing_type") or sourcing_type)
            log(f"  💾 DB 저장: 공급처 {n}곳 (상위 {min(n, TOP_PICKS)}곳 추천 표시) · 구매 사이트 {nc}곳 · 리포트 {report}")
            item = {"product_id": p["id"], "product": p["name"], "suppliers": n,
                    "run_dir": v["run_dir"], "top3": v["top3"]}
            if to_buyagent and (payload.get("sourcing_type") or sourcing_type) == "retail":
                item["buyagent"] = export_to_buyagent(v["run_dir"], log=log)
            done.append(item)
            run.report_dir = v["run_dir"]
    return {"run_id": run.id, "processed": done}
