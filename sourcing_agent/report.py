"""⑤ 리포트: results.json(다음 단계 에이전트용) + CSV(엑셀용) + report.md / report.html(사람용)."""
import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

from .quality import quality_text
from .packaging import packaging_text
from .price_basis import price_basis_text

# results.json 형식이 바뀌면 올립니다. 다음 단계 에이전트는 이 값을 확인하고 읽으세요.
#   1.x: 필드 추가만 (하위 호환)   2.0: 필드 삭제/의미 변경
SCHEMA_VERSION = "1.5"   # 1.1: suppliers[].specs (제품 사양) · 1.2: suppliers[].packaging (포장·무게) · 1.4: suppliers[].price_basis · 1.5: suppliers[].model·quality (제품 품질·리뷰)
#   1.3: request.market_listings (AliExpress·eBay 공식 API 판매 상품) · suppliers[].market_listing. 모르면 null


def _j(v):
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x is not None)
    return "" if v is None else str(v)


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _d(r, key) -> dict:
    v = r.get(key)
    return v if isinstance(v, dict) else {}


def _evidence_line(e) -> str:
    if isinstance(e, dict):
        url = e.get("url")
        return f"  - {e.get('claim')}" + (f" ({url})" if url else "")
    return f"  - {e}"


def _md_cell(v) -> str:
    return _j(v).replace("|", "/").replace("\n", " ")


def _contact_line(c: dict) -> str:
    parts = [f"{label} {c[k]}" for k, label in
             (("contact_person", "담당"), ("email", "이메일"), ("phone", "전화"), ("messenger", "메신저"))
             if c.get(k)]
    return " / ".join(parts) or "미확인"


def build_payload(results: list[dict], request: dict, channels: list[dict] | None = None) -> dict:
    """다음 단계(3 통관·규제 → 4 원가·수익 → 5 협상) 에이전트에 넘길 고정 형식."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agent": "trade_sourcing_agent",
        "mode": request.get("mode", "supplier"),
        "sourcing_type": request.get("sourcing_type", "oem"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request": request,  # 품목, hs_code, 목표 단가 등 — 3번 통관·규제 체크 이후 단계까지 그대로 전달
        "suppliers": [{"rank": i, **r} for i, r in enumerate(results, 1)],
        # 구매 사이트 순위 (1.x 추가 필드): [{rank, key, site, search_url, total, scores, typical_price, ...}]
        "channels": channels or [],
    }


def write_reports(results: list[dict], request: dict, out_dir, channels: list[dict] | None = None) -> dict:
    results = sorted(results, key=lambda r: r.get("total") or 0, reverse=True)
    channels = channels or []
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "results.json"
    json_path.write_text(json.dumps(build_payload(results, request, channels), ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # 엑셀에서 한글이 깨지지 않도록 utf-8-sig
    csv_path = out_dir / "results.csv"
    cols = ["순위", "업체명", "국가", "유형", "업력", "인증", "MOQ", "단가대",
            "총점", "등급", "리스크", "미확인", "수동정보",
            "담당자", "이메일", "전화", "메신저", "리드타임", "결제조건", "샘플", "한국 배송", "포장·무게", "가격 기준", "제품 품질", "링크"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, r in enumerate(results, 1):
            c, t = _d(r, "contacts"), _d(r, "trade_terms")
            w.writerow([i, r.get("name"), r.get("country"), r.get("company_type"),
                        r.get("years_in_business"), _j(r.get("certifications")), r.get("moq"),
                        r.get("price_range"), r.get("total"), r.get("grade"),
                        _j(r.get("risk_flags")), _j(r.get("unverified")),
                        "O" if r.get("manual_info_used") else "",
                        c.get("contact_person"), c.get("email"), c.get("phone"), c.get("messenger"),
                        t.get("lead_time"), t.get("payment_terms"), t.get("sample"), _korea_ship(t), packaging_text(r.get("packaging")),
                        price_basis_text(r.get("price_basis")), quality_text(r.get("quality")),
                        _j(r.get("urls"))])

    rubric = _rubric(request)
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(results, request, rubric, channels), encoding="utf-8")
    html_path = out_dir / "report.html"
    html_path.write_text(render_html(results, request, rubric, channels), encoding="utf-8")

    return {"csv": str(csv_path), "markdown": str(md_path), "html": str(html_path), "json": str(json_path)}


# ---------------------------------------------------------------------------
# 사람이 읽는 리포트 (report.md / report.html) — 내용은 같고 형식만 다름
# ---------------------------------------------------------------------------
TYPE_KO = {"manufacturer": "제조사", "trading": "무역상", "unknown": "유형 미확인"}
DISCLAIMER = ("AI 검증은 1차 거르기용입니다. 거래 전 샘플 주문, 화상 공장투어, "
              "제3자 실사(SGS·QIMA 등)로 최종 확인하세요.")


def _rubric(request):
    try:
        from .scoring import load_rubric
        return load_rubric(request.get("sourcing_type", "oem"))
    except Exception:          # 점수표를 못 읽어도 리포트는 만든다 (항목 키로 표시)
        return {}


def _num(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "–" if v is None else str(v)


def _short(v, n=40) -> str:
    t = _j(v).replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def _type_ko(r) -> str:
    return TYPE_KO.get(str(r.get("company_type")), _j(r.get("company_type")))


def _score_parts(r, rubric):
    """[(라벨, 점수, 만점)], 리스크 감점."""
    items = rubric.get("items") or {}
    scores = _d(r, "scores")
    keys = list(items) or list(scores)
    return [(items.get(k, {}).get("label", k), scores.get(k), items.get(k, {}).get("max")) for k in keys], r.get("risk_penalty")


def _request_lines(request):
    rows = [("목표 단가", request.get("target_price")),
            ("첫 구매 수량", request.get("max_order_qty") or request.get("moq")),
            ("필요 인증", _j(request.get("required_certs"))),
            ("선호 국가", _j(request.get("preferred_countries"))),
            ("HS 코드", request.get("hs_code")),
            ("마켓 실판매가", _market_line(request.get("market_listings")))]
    return [(k, _j(v)) for k, v in rows if v]


def _korea_ship(t):
    """trade_terms.ships_to_korea / korea_shipping → '직배송 가능 (EMS 2,000엔)' / '국내배송만 — 배송대행지 필요'."""
    v, how = t.get("ships_to_korea"), t.get("korea_shipping")
    head = "직배송 가능" if v is True else "국내배송만 — 배송대행지 필요" if v is False else ("" if not how else "확인 필요")
    return f"{head} ({how})" if head and how else head or _j(how)


def _market_line(m):
    """공식 마켓 API 요약 한 줄 (AliExpress 2,345개 · $2.5~$9.9, 중간 $4.2). 없으면 None."""
    if not isinstance(m, dict) or not m.get("price_usd"):
        return None
    p = m["price_usd"]
    return (f"{m.get('source')} '{m.get('keyword')}' 상품 {m.get('total') or 0:,}개 · "
            f"${p['min']:g}~${p['max']:g} (중간 ${p['median']:g}) — 공식 API")


def _facts(r):
    c, t = _d(r, "contacts"), _d(r, "trade_terms")
    rows = [
        ("유형·지역", " · ".join(x for x in [_type_ko(r), _j(r.get("country")), _j(r.get("city"))] if x)),
        ("업력", _j(r.get("years_in_business"))), ("MOQ", _j(r.get("moq"))), ("단가", _j(r.get("price_range"))),
        ("가격 기준", price_basis_text(r.get("price_basis"))),
        ("인증", _j(r.get("certifications"))), ("플랫폼 배지", _j(r.get("platform_badges"))),
        ("연락처", _contact_line(c) if any(c.get(k) for k in c) else ""),
        ("리드타임", _j(t.get("lead_time"))), ("결제조건", _j(t.get("payment_terms"))), ("샘플", _j(t.get("sample"))),
        ("한국 배송", _korea_ship(t)),
        ("포장·무게", packaging_text(r.get("packaging"))),
        ("제품 품질", quality_text(r.get("quality"))),
    ]
    return [(k, v) for k, v in rows if v]


def _urls(r) -> list:
    return [u for u in (_as_list(r.get("urls")) or _as_list(r.get("website"))) if u]


def _channel_labels() -> dict:
    try:
        from .channels import load_channels
        return {k: v.get("label", k) for k, v in load_channels()["items"].items()}
    except Exception:
        return {}


def _channel_parts(c, labels) -> str:
    return " · ".join(f"{labels.get(k, k)} {_num(v)}" for k, v in _d(c, "scores").items())


def render_markdown(results, request, rubric, channels=None) -> str:
    label = rubric.get("label", request.get("sourcing_type", "oem"))
    md = [f"# 공급처 발굴 리포트 — {request.get('product')}", "",
          f"> 구매 방식 **{label}** · 검증 {len(results)}곳 · {datetime.now():%Y-%m-%d %H:%M}", ""]
    req = _request_lines(request)
    if req:
        md += ["## 요청 조건", ""] + [f"- **{k}**: {_md_cell(v)}" for k, v in req] + [""]
    if channels:
        labels = _channel_labels()
        md += ["## 구매 사이트 순위", "", "| 순위 | 사이트 | 점수 | 소량 단가 | 최소 수량 | 한 줄 평 |",
               "|---:|---|---:|---|---|---|"]
        for c in channels:
            md.append(f"| {c.get('rank')} | [{_md_cell(c.get('site'))}]({c.get('search_url')}) | **{_num(c.get('total'))}** | "
                      f"{_md_cell(c.get('typical_price')) or '–'} | {_md_cell(c.get('min_qty')) or '–'} | "
                      f"{_md_cell(_short(c.get('summary'), 70)) or '–'} |")
        md += ["", "<details><summary>사이트별 점수 내역</summary>", ""]
        md += [f"- **{c.get('site')}**: {_channel_parts(c, labels)}" + (f" — {c['site_note']}" if c.get("site_note") else "")
               for c in channels]
        md += ["", "</details>", ""]
    md += ["## 한눈에 보기", "",
           "| 순위 | 업체 | 유형 | 점수 | MOQ | 단가 | 위험 |", "|---:|---|---|---:|---|---|---:|"]
    for i, r in enumerate(results, 1):
        md.append(f"| {i} | {_md_cell(r.get('name'))} | {_md_cell(_type_ko(r))} | "
                  f"**{_num(r.get('total'))}** ({r.get('grade')}) | {_md_cell(_short(r.get('moq'), 24)) or '–'} | "
                  f"{_md_cell(_short(r.get('price_range'), 24)) or '–'} | {len(_as_list(r.get('risk_flags')))}건 |")
    md += ["", "## 업체별 상세", ""]
    for i, r in enumerate(results, 1):
        md += [f"### {i}. {r.get('name')} — {_num(r.get('total'))}점 ({r.get('grade')})", ""]
        if r.get("summary"):
            md += [f"> {r['summary']}", ""]
        md += [f"- **{k}**: {v}" for k, v in _facts(r)]
        if _urls(r):
            md.append("- **링크**: " + " · ".join(f"[{j}]({u})" for j, u in enumerate(_urls(r), 1)))
        parts, penalty = _score_parts(r, rubric)
        if parts:
            line = " · ".join(f"{lbl} {_num(sc)}" + (f"/{mx}" if mx else "") for lbl, sc, mx in parts)
            md += ["", f"**점수 내역**: {line}" + (f" · 리스크 감점 {_num(penalty)}" if penalty else "")]
        for head, key, ordered in (("위험 요소", "risk_flags", False), ("미확인", "unverified", False),
                                   ("첫 문의 때 물어볼 것", "first_questions", True)):
            items = _as_list(r.get(key))
            if items:
                md += ["", f"**{head}**", ""]
                md += [(f"{n}. " if ordered else "- ") + str(x) for n, x in enumerate(items, 1)]
        ev = _as_list(r.get("evidence"))
        if ev:
            md += ["", f"<details><summary>근거 {len(ev)}건</summary>", ""] + [_evidence_line(e) for e in ev]
            md += ["", "</details>"]
        md += ["", "---", ""]
    md.append(f"※ {DISCLAIMER}")
    return "\n".join(md)


def _h(v) -> str:
    return escape(_j(v))


def _li(items, cls="", ordered=False) -> str:
    tag = "ol" if ordered else "ul"
    return f'<{tag} class="{cls}">' + "".join(f"<li>{_h(x)}</li>" for x in items) + f"</{tag}>"


def _a(url, label) -> str:
    return f'<a href="{escape(str(url), quote=True)}" target="_blank" rel="noopener">{escape(str(label))}</a>'


def _ev_html(e) -> str:
    if isinstance(e, dict):
        return f"<li>{_h(e.get('claim'))}" + (f" · {_a(e['url'], '출처')}" if e.get("url") else "") + "</li>"
    return f"<li>{_h(e)}</li>"


HTML_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--soft:#e8efff;
--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--bar:#dbe3f4}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e5e7eb;--muted:#9aa3b2;--line:#2a303b;
--accent:#7aa2ff;--soft:#1d2a4a;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--bar:#262d3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:0 0 12px}.muted{color:var(--muted)}
section,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}
.req{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}.req dt{color:var(--muted)}.req dd{margin:0}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);white-space:nowrap}tr:last-child td{border-bottom:0}
td.n{text-align:right;white-space:nowrap}a{color:var(--accent)}
.g{display:inline-block;min-width:22px;text-align:center;border-radius:6px;padding:0 6px;font-weight:700;font-size:12px;color:#fff}
.gA{background:var(--ok)}.gB{background:var(--accent)}.gC{background:var(--warn)}.gD{background:var(--bad)}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 12px}.head h3{margin:0;font-size:17px}
.total{font-size:22px;font-weight:700;color:var(--accent);margin-left:auto}
.summary{background:var(--soft);border-radius:8px;padding:10px 12px;margin:10px 0}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 18px;margin:8px 0}
.facts div span{color:var(--muted);font-size:12px;display:block}
.bars{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px 18px;margin:12px 0}
.bar{font-size:12px}.bar i{display:block;height:6px;border-radius:3px;background:var(--bar);margin-top:3px;overflow:hidden}
.bar i b{display:block;height:100%;background:var(--accent)}.bar .minus{color:var(--bad)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:4px 20px}
h4{margin:12px 0 4px;font-size:13px}ul,ol{margin:0;padding-left:20px}li{margin:3px 0}
ul.risk li::marker{color:var(--warn)}h4.risk{color:var(--warn)}
details summary{cursor:pointer;color:var(--muted);margin-top:12px}.note{color:var(--muted);font-size:12px}
.small{font-size:12px}.cnt{color:var(--warn);font-weight:600;white-space:nowrap}
tr.row{cursor:pointer}tr.row:hover td,tr.row.open td{background:var(--soft)}tr.row:focus{outline:2px solid var(--accent)}
.tog{display:inline-block;color:var(--muted);transition:transform .15s}tr.row.open .tog{transform:rotate(90deg)}
tr.detail>td{padding:0 0 10px;background:var(--soft)}tr.detail .card{margin:0 8px;border-radius:10px}
.chips{margin-top:4px}.chip{display:inline-block;font-size:11px;color:var(--muted);border:1px solid var(--line);
border-radius:999px;padding:0 7px;margin:2px 4px 0 0;white-space:nowrap}
h2 button{float:right;font:inherit;font-size:12px;padding:3px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);cursor:pointer}
"""


def _channels_html(channels) -> str:
    if not channels:
        return ""
    labels = _channel_labels()
    rows = []
    for c in channels:
        bars = "".join(f'<span class="chip">{escape(str(labels.get(k, k)))} {_num(v)}</span>' for k, v in _d(c, "scores").items())
        ev = _as_list(c.get("evidence"))
        ev_html = f"<details><summary>근거 {len(ev)}건</summary><ul>{''.join(_ev_html(e) for e in ev)}</ul></details>" if ev else ""
        rows.append(
            f'<tr><td class="n">{c.get("rank")}</td>'
            f'<td><b>{_a(c["search_url"], c.get("site"))} ↗</b>'
            f'<div class="muted small">{_h(c.get("site_note"))}</div></td>'
            f'<td class="n"><b>{_num(c.get("total"))}</b></td>'
            f'<td>{_h(c.get("typical_price")) or "–"}</td><td>{_h(c.get("min_qty")) or "–"}</td>'
            f'<td>{_h(c.get("summary"))}<div class="chips">{bars}</div>{ev_html}</td></tr>')
    return (f'<section><h2>구매 사이트 순위 <span class="muted small">사이트 이름을 누르면 이 품목 검색 결과가 열립니다</span></h2>'
            f'<div class="scroll"><table><thead><tr><th>순위</th><th>사이트</th><th>점수</th><th>소량 단가</th>'
            f'<th>최소 수량</th><th>평가</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def render_html(results, request, rubric, channels=None) -> str:
    label = rubric.get("label", request.get("sourcing_type", "oem"))
    rows = []
    for i, r in enumerate(results, 1):
        parts, penalty = _score_parts(r, rubric)
        bars = "".join(
            f'<div class="bar">{escape(str(lbl))} <b>{_num(sc)}</b>{f"/{mx}" if mx else ""}'
            f'<i><b style="width:{(min(100, max(0, (sc or 0) / mx * 100)) if mx else 0):.0f}%"></b></i></div>'
            for lbl, sc, mx in parts)
        if penalty:
            bars += f'<div class="bar">리스크 감점 <b class="minus">{_num(penalty)}</b></div>'
        facts = "".join(f"<div><span>{escape(k)}</span>{escape(v)}</div>" for k, v in _facts(r))
        if _urls(r):
            facts += "<div><span>링크</span>" + " · ".join(_a(u, j) for j, u in enumerate(_urls(r), 1)) + "</div>"
        risk, unv, qs = (_as_list(r.get(k)) for k in ("risk_flags", "unverified", "first_questions"))
        left = (f'<h4 class="risk">⚠ 위험 요소 {len(risk)}건</h4>{_li(risk, "risk")}' if risk else "") + \
               (f"<h4>미확인</h4>{_li(unv)}" if unv else "")
        right = f"<h4>첫 문의 때 물어볼 것</h4>{_li(qs, ordered=True)}" if qs else ""
        ev = _as_list(r.get("evidence"))
        ev_html = f"<details><summary>근거 {len(ev)}건</summary><ul>{''.join(_ev_html(e) for e in ev)}</ul></details>" if ev else ""
        summary = f'<div class="summary">{_h(r.get("summary"))}</div>' if r.get("summary") else ""
        first_risk = (f'<span class="small">{escape(_short(risk[0], 46))}</span> ' if risk else "") + \
                     (f'<span class="cnt">{len(risk)}건</span>' if risk else "–")
        # 요약 줄 (클릭하면 바로 아래 상세 줄이 펼쳐짐)
        rows.append(
            f'<tr class="row" data-i="{i}" tabindex="0"><td class="n">{i}</td>'
            f'<td><span class="tog">▸</span> <b>{_h(r.get("name"))}</b><div class="muted small">{_h(r.get("country"))}</div></td>'
            f'<td>{escape(_type_ko(r))}</td><td class="n"><b>{_num(r.get("total"))}</b></td>'
            f'<td><span class="g g{_h(r.get("grade"))}">{_h(r.get("grade"))}</span></td>'
            f'<td>{escape(_short(r.get("moq"), 26)) or "–"}</td><td>{escape(_short(r.get("price_range"), 26)) or "–"}</td>'
            f'<td>{first_risk}</td></tr>')
        rows.append(
            f'<tr class="detail" id="s{i}" hidden><td colspan="8"><div class="card">{summary}'
            f'<div class="facts">{facts}</div><div class="bars">{bars}</div>'
            f'<div class="cols"><div>{left}</div><div>{right}</div></div>{ev_html}</div></td></tr>')

    req = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in _request_lines(request))
    notes = (f'<details><summary>요청 메모</summary><div style="white-space:pre-wrap">{_h(request.get("notes"))}</div></details>'
             if request.get("notes") else "")
    req_section = f'<section><h2>요청 조건</h2><dl class="req">{req}</dl>{notes}</section>' if req or notes else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>공급처 리포트 — {_h(request.get("product"))}</title><style>{HTML_CSS}</style></head>
<body><main>
<h1>공급처 발굴 리포트</h1>
<div class="muted">{_h(request.get("product"))} · {escape(str(label))} · 검증 {len(results)}곳 · {datetime.now():%Y-%m-%d %H:%M}</div>
{req_section}
{_channels_html(channels)}
<section><h2>공급처 {len(results)}곳 <span class="muted small">업체를 누르면 상세가 펼쳐집니다</span>
<button type="button" id="all">모두 펼치기</button></h2>
<div class="scroll"><table class="main"><thead><tr><th>순위</th><th>업체</th><th>유형</th><th>총점</th><th>등급</th>
<th>MOQ</th><th>단가</th><th>리스크</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>
<p class="note">※ {escape(DISCLAIMER)}</p>
</main>
<script>
function toggle(i, open) {{
  const d = document.getElementById("s" + i), row = document.querySelector('tr.row[data-i="' + i + '"]');
  if (!d || !row) return;
  d.hidden = open === undefined ? !d.hidden : !open;
  row.classList.toggle("open", !d.hidden);
}}
document.querySelectorAll("tr.row").forEach(function (row) {{
  row.addEventListener("click", function (ev) {{ if (!ev.target.closest("a")) toggle(row.dataset.i); }});
  row.addEventListener("keydown", function (ev) {{
    if (ev.key === "Enter" || ev.key === " ") {{ ev.preventDefault(); toggle(row.dataset.i); }}
  }});
}});
const allBtn = document.getElementById("all");
allBtn.addEventListener("click", function () {{
  const open = allBtn.textContent === "모두 펼치기";
  document.querySelectorAll("tr.row").forEach(function (row) {{ toggle(row.dataset.i, open); }});
  allBtn.textContent = open ? "모두 접기" : "모두 펼치기";
}});
const m = location.hash.match(/^#s(\\d+)$/);      // 대시보드에서 #s2로 열면 그 업체를 펼침
if (m) {{ toggle(m[1], true); const row = document.querySelector('tr.row[data-i="' + m[1] + '"]'); if (row) row.scrollIntoView(); }}
</script>
</body></html>"""


def add_channels(run_dir, llm, log=None) -> dict:
    """이미 만든 실행 폴더에 구매 사이트 순위만 추가 (API 호출 1회). 공급처 결과는 그대로."""
    from .channels import assess_channels
    run_dir = Path(run_dir)
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    channels = assess_channels(llm, payload["request"], log=log)
    results = [{k: v for k, v in s.items() if k != "rank"} for s in payload["suppliers"]]
    files = write_reports(results, payload["request"], run_dir, channels=channels)
    return {"run_dir": str(run_dir), "files": files,
            "channels": [{"site": c["site"], "total": c["total"], "search_url": c["search_url"]} for c in channels]}


def rebuild(run_dir) -> dict:
    """이미 만든 실행 폴더의 results.json으로 리포트(md·html·csv)를 다시 만든다 (API 호출 없음)."""
    run_dir = Path(run_dir)
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    results = [{k: v for k, v in s.items() if k != "rank"} for s in payload["suppliers"]]
    return write_reports(results, payload["request"], run_dir, channels=payload.get("channels"))
