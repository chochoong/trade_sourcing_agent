"""공용 DB(trade_hub) 연동.

1번 에이전트가 찾고 사람이 `trade_hub select`로 넘긴 품목(stage=sourcing)을 DB에서 읽어
공급처를 찾고, 결과를 suppliers·product_suppliers에 기록한다.

  python -m sourcing_agent from-db              # 아직 조사하지 않은 sourcing 품목 전부
  python -m sourcing_agent from-db 16 17        # 지정한 품목만 (이미 조사했어도 다시)

이 에이전트가 쓰는 곳: suppliers, product_suppliers, tasks(검토 요청), product_events(조사 기록).
단계(stage)는 바꾸지 않는다 — 공급처를 골라 다음 단계(3번 통관·규제 체크 → 4번 원가 → 5번 협상)로 넘기는 건 사람/다음 에이전트의 일.
"""
import json
import re
from contextlib import contextmanager
from pathlib import Path

from trade_hub import db

from .agent import log_stderr, run_discover, run_verify
from .discover import parse_qty, parse_years
from .errors import SourcingError, friendly
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


MIXED_MARK = "[변형 혼합]"   # 1번이 판매가가 다른 변형을 별도 품목으로 나눈 원래 품목 (notes 첫머리)


def split_into(p):
    """'[변형 혼합]' 품목이면 나눈 품목 번호 목록 (notes 의 #64 등), 아니면 None."""
    notes = str(product_field(p, "notes") or "")
    if not notes.startswith(MIXED_MARK):
        return None
    return sorted({int(x) for x in re.findall(r"#(\d+)", notes)} - {p["id"]})


def _skip_mixed(rows, log=None):
    """변형 혼합 품목은 판매가(est_retail_krw)가 섞인 값이라 단가 상한이 틀림 → 조사하지 않고 나눈 품목을 안내."""
    log = log or log_stderr
    ok = []
    for r in rows:
        parts = split_into(r)
        if parts is None:
            ok.append(r)
        else:
            log(f"  · 건너뜀: [{r['id']}] {r['name']} — 변형 혼합 품목 (1번이 나눔). "
                f"나눈 품목을 조사하세요: {', '.join(f'#{i}' for i in parts) or 'notes 참고'}")
    return ok


def pending_products(conn, ids=None):
    if not ids:
        return _skip_mixed(conn.execute(PENDING_SQL).fetchall())
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
    return _skip_mixed(ok)


# 공급 국가별 추가 지시 (조사 조건 notes 에 덧붙임)
COUNTRY_NOTES = {
    "Japan": "- 공급 국가: 일본. 검색은 품목명을 일본어로 번역한 검색어로 하고, 가격은 엔화 원문과 USD 환산을 함께 적을 것. "
             "해외(한국) 구매자 거래·해외 배송 가능 여부(안 되면 배송대행지 필요)를 확인할 것.\n"
             "- 일본 판매자는 대부분 국내배송만 하므로 **해외 직배송 판매자를 우선** 찾을 것: 검색어에 「海外発送」「海外配送 対応」"
             "「international shipping」을 붙인 검색(예: `<일본어 품목명> 海外発送`), 브랜드·메이커 공식 온라인숍의 해외 판매 페이지"
             "(영문 English/Global 사이트, Shopify·BASE·STORES 의 Worldwide shipping), Amazon.co.jp 의 해외 발송 가능 상품, "
             "라쿠텐·Yahoo!ショッピング 스토어의 「海外配送」 안내를 확인. 후보의 절반 이상은 해외 직배송 판매자로 채우고, "
             "국내배송만 되는 곳도 빼지 말고 ships_to_korea=false 로 적을 것.",
    "USA": "- 공급 국가: 미국. 검색은 영문 검색어로 eBay·Amazon.com·미국 제조사/도매상(Thomasnet 등)에서 하고, 가격은 USD로 적을 것. "
           "한국 배송(국제 배송·eBay GSP) 가능 여부(안 되면 배송대행지 필요)와 판매자 피드백 점수·누적 거래를 확인할 것.",
}
COUNTRIES = ("China", "Japan", "USA")   # 2번이 조사할 수 있는 공급 국가 (trade_hub products.source_country 값과 같음)
DEFAULT_COUNTRY = "China"               # 지정도 없고 1번 추천 국가도 없을 때만


def product_field(p, key):
    """DB 행에서 값 읽기 (예전 스키마라 컬럼이 없으면 None)."""
    try:
        return p[key]
    except (IndexError, KeyError):
        return None


def product_country(p, country=None):
    """조사할 국가: 명시한 값 > 1번이 추천한 품목의 source_country > China."""
    if country:
        return country
    rec = product_field(p, "source_country")
    return rec if rec in COUNTRIES else DEFAULT_COUNTRY


def reference_url(p):
    """1번의 참고 상품 페이지(products.ref_url). http(s) 주소만, 없으면 None."""
    try:
        url = (p["ref_url"] or "").strip()
    except (IndexError, KeyError):
        return None
    return url if re.match(r"https?://", url) else None


def _pct(v, default):
    """products.duty_rate (DB 는 0~1 비율, 예: 0.08) → 퍼센트 숫자 8. 1보다 크면 이미 퍼센트로 봄. 모르면 default."""
    try:
        x = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return float(default)
    return x * 100 if x <= 1 else x


def price_ceiling(p, sourcing_type="small_batch"):
    """소량 사입: 국내 판매가에서 역산한 '이 값 이하로 사야 마진이 남는' 개당 단가 상한 (USD, 설명). 없으면 None."""
    cfg = (load_rubric(sourcing_type) or {}).get("price_ceiling")
    retail = product_field(p, "est_retail_krw")
    if not cfg or not retail:
        return None
    duty = _pct(product_field(p, "duty_rate"), cfg.get("duty_pct", 8))
    ship = product_field(p, "est_shipping_usd")
    freight = float(ship) if isinstance(ship, (int, float)) and ship >= 0 else float(cfg.get("freight_usd", 0.5))
    vat, fee, margin, fx = (float(cfg.get(k, d)) for k, d in
                            (("vat_pct", 10), ("fee_pct", 12), ("margin_pct", 25), ("usd_krw", 1400)))
    usd = retail / (1 + vat / 100) * (1 - (fee + margin) / 100) / fx / (1 + duty / 100) - freight
    if usd <= 0:
        return 0.0, (f"국내 판매가 {retail:,}원으로는 수수료 {fee:g}%·마진 {margin:g}%·관세 {duty:g}%·운임 ${freight:g} 를 "
                     "빼면 남는 단가가 없음 (판매가를 다시 확인할 것)")
    return round(usd, 2), (f"소량 구매 단가 상한 약 USD {usd:.2f} — 국내 판매가 {retail:,}원에서 부가세·수수료 {fee:g}%·"
                           f"목표 마진 {margin:g}%·관세 {duty:g}%·개당 운임 ${freight:g} 를 뺀 추정값 (이 값 이하면 마진이 남음)")


def build_request(conn, p, sourcing_type="small_batch", max_order_qty=None, country=None):
    """DB 품목 → 소싱 조건 dict (YAML 요청 파일과 같은 형식). country 없으면 1번 추천 국가."""
    country = product_country(p, country)
    s = conn.execute("SELECT est_fob_usd FROM product_scores WHERE product_id = ? ORDER BY id DESC LIMIT 1",
                     (p["id"],)).fetchone()
    fob = s["est_fob_usd"] if s else None
    price = []
    ceiling = price_ceiling(p, sourcing_type) if sourcing_type == "small_batch" else None
    if ceiling:   # 소량 사입: FOB(수백 개 이상 공장 단가) 대신 판매가에서 역산한 상한으로 단가를 봄
        price.append(ceiling[1])
        if fob:
            price.append(f"참고: 대량 FOB 약 USD {fob:g} (시장조사 웹 시세, 수백 개 이상 기준 — 소량 단가와 직접 비교하지 말 것)")
    else:
        if fob:
            price.append(f"FOB 약 USD {fob:g} 내외 (시장조사 단계 웹 시세)")
        if p["est_retail_krw"]:
            price.append(f"국내 판매가 약 {p['est_retail_krw']:,}원 — 마진이 남는 단가일 것")
    notes = [f"- {label}: {p[k]}" for k, label in
             (("reason", "선정 이유"), ("regulations", "예상 인증·규제 (판매자가 시험성적서를 줄 수 있는지 확인)"),
              ("risk", "위험 요소"), ("notes", "메모")) if p[k]]
    if COUNTRY_NOTES.get(country):
        notes.append(COUNTRY_NOTES[country])
    seed = product_field(p, "source_page_url")
    if seed:   # 1번이 조사 중 본 해외 구매처 상품 상세 페이지 → 이 판매자부터 검증
        notes.append(f"- 시장조사 단계에서 본 해외 판매 페이지: {seed} — 이 판매자를 첫 후보로 포함해 검증할 것 "
                     "(조건에 안 맞거나 검증에서 문제가 나오면 그 이유를 적고 다른 후보와 똑같이 평가)")
    ref = reference_url(p)
    req = {
        "mode": "supplier",
        "sourcing_type": sourcing_type,
        "product": p["name"],
        "product_en": p["search_keyword_en"] or p["name"],
        "hs_code": p["hs_code"],
        # 1번이 시장조사 중 본 대표 상품 페이지 → 같은 물건(사양)을 찾는 기준. 구매처 상품 페이지면 그 판매자도 후보로
        "reference_urls": [ref] if ref else None,
        "target_price": " / ".join(price) or None,
        "price_ceiling_usd": ceiling[0] if ceiling else None,
        "required_certs": [],          # 인증 확정은 3번(통관·규제 체크) 단계 — 여기서는 notes로만 전달
        "preferred_countries": [country],
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


def _old_packaging(conn, product_id, supplier_id, purchase_mode=None):
    """예전 조사에서 저장한 포장·무게 (같은 품목·공급처, 같은 구매 방식 줄 우선 — 줄은 구매 방식별로 따로). 없으면 None."""
    row = conn.execute("SELECT trade_terms_json FROM product_suppliers WHERE product_id = ? AND supplier_id = ?"
                       " AND trade_terms_json LIKE '%\"packaging\"%' ORDER BY (purchase_mode IS ?) DESC, updated_at DESC",
                       (product_id, supplier_id, purchase_mode)).fetchone()
    try:
        old = json.loads(row["trade_terms_json"]) if row and row["trade_terms_json"] else {}
    except (TypeError, ValueError):
        return None
    return old.get("packaging") if isinstance(old, dict) else None


def save_results(conn, run_id, product_id, suppliers, report_path, purchase_mode=None, max_order_qty=None):
    """results.json의 suppliers → suppliers + product_suppliers (+ 사람 검토 할 일).

    purchase_mode·max_order_qty: 조사 때 구매 방식(retail / small_batch / oem)과 첫 구매 최대 수량
    — 대시보드가 MOQ 배지(소량 OK / 초과 / 미확인)의 기준으로 씀.
    """
    with conn:
        # 같은 구매 방식으로 다시 조사하면 예전 실행의 추천 표시를 지움 → 추천 = 최신 실행의 상위 3곳만.
        # (3번이 추천 중에서 대표 공급처를 고르므로, 쌓이면 사양 없는 예전 업체가 대표가 됨)
        # 이번에 하나도 못 찾았으면 예전 추천을 그대로 둠. 사람이 고른 selected 는 건드리지 않음.
        if suppliers:
            conn.execute("UPDATE product_suppliers SET is_top_pick = 0 WHERE product_id = ? AND is_top_pick = 1"
                         " AND purchase_mode IS ?", (product_id, purchase_mode))
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
            basis = r.get("price_basis") if isinstance(r.get("price_basis"), dict) else None
            link = {"is_top_pick": int(i < TOP_PICKS), "moq": _text(r.get("moq")),
                    # 단가 + 어떤 상품의 단가인지 (예: "USD 0.53-0.79 — 4구 LED형"). 4번이 한 품목 안의 다른 변형을 가려냄
                    "price_note": " — ".join(x for x in [_text(r.get("price_range")), (basis or {}).get("variant")] if x)
                    or None,
                    # v4: 이 품목 기준 평가 결과 (5번 협상은 first_questions·trade_terms 를 씀)
                    "purchase_mode": purchase_mode, "max_order_qty": max_order_qty,
                    "score": r.get("total"), "grade": r.get("grade") if r.get("grade") in TRUST else None,
                    "score_detail_json": r.get("scores") if isinstance(r.get("scores"), dict) else None,
                    "trade_terms_json": r.get("trade_terms") if isinstance(r.get("trade_terms"), dict) else None,
                    "first_questions_json": _list(r.get("first_questions")),
                    "unverified_json": _list(r.get("unverified")),
                    "evidence_json": _list(r.get("evidence"))}
            # 원가·수익 계산(4번)은 unit_price_usd 가 있는 업체만 처리함 → 문장형 단가에서 USD 숫자를 뽑아 넣음
            price = r.get("unit_price_usd") or parse_usd_price(r.get("price_range"))
            if price:
                link["unit_price_usd"] = price
            # 포장·무게 → trade_terms_json.packaging (4번 원가 계산이 운임 추정에 씀). 이번에 모르면 예전 값 유지
            if link["trade_terms_json"] is not None or r.get("packaging") or basis:
                terms = dict(link["trade_terms_json"] or {})
                terms["packaging"] = r.get("packaging") or _old_packaging(conn, product_id, sid, purchase_mode)
                if terms["packaging"] is None:
                    del terms["packaging"]
                # 가격 기준 (통화·배송비 포함 여부·공장/도매/소매·변형) → 4번. 가격은 바뀌므로 예전 값은 이어받지 않음
                terms.pop("price_basis", None)
                if basis:
                    terms["price_basis"] = basis
                link["trade_terms_json"] = terms
            # 제품 사양 (trade_hub v13~, 3번 통관·규제 체크가 읽음). 모르면 저장하지 않음 (예전 값 유지)
            if r.get("specs") and "specs_json" in db.LINK_FIELDS:
                link["specs_json"] = r["specs"]
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
    from .buyagent_export import BuyagentNotFound, export
    try:
        r = export(run_dir, write=True)
    except (BuyagentNotFound, ValueError) as e:
        msg = str(e)
    except OSError as e:
        msg = (f"buyagent 파일을 읽거나 쓰지 못했습니다{f' ({e.filename})' if e.filename else ''}. "
               "파일이 엑셀 등 다른 프로그램에서 열려 있으면 닫고 다시 내보내세요. 조사 결과는 그대로 저장되어 있습니다.")
    except Exception as e:
        msg = f"buyagent 내보내기 실패: {friendly(e)} 조사 결과는 그대로 저장되어 있습니다."
    else:
        msg = None
    if msg:
        log(f"  ⚠ buyagent 내보내기 생략: {msg}")
        return {"error": msg}
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


@contextmanager
def _readable_errors():
    """실패를 SourcingError(한국어 문구)로 바꿔 올림 → trade_hub 가 agent_runs.error 에 그 문구를 남김.
    원래 예외는 __cause__ 로 이어 둠."""
    try:
        yield
    except SourcingError:
        raise
    except Exception as e:
        raise SourcingError(friendly(e)) from e


def run_from_db(ids=None, sourcing_type="small_batch", max_order_qty=None, dry_run=False, log=log_stderr,
                to_buyagent=True, country=None):
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
    requests = [(p, build_request(conn, p, sourcing_type, max_order_qty, country)) for p in products]
    if dry_run:
        return {"dry_run": [r for _, r in requests]}

    done = []
    args = {"ids": ids, "sourcing_type": sourcing_type, "max_order_qty": max_order_qty, "country": country}
    with db.agent_run(conn, AGENT, provider="anthropic", model=LLM().model, args=args) as run, \
            _readable_errors():
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
