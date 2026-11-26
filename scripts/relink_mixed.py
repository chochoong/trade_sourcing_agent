"""1번이 '[변형 혼합]'으로 나눈 품목의 공급처 줄을 나뉜 품목으로 다시 연결 (4번 요청, 2026-11-26).

  python scripts/relink_mixed.py          # 미리보기 (DB 안 바꿈)
  python scripts/relink_mixed.py --apply  # 반영

- 줄마다 공급처 사양(상품 페이지 근거)을 보고 사람이 판단한 MOVES 표대로 새 품목에 link_supplier 로 연결
  (근거·사양·포장·첫 질문·확인 못한 항목은 그대로, price_basis.variant·단가는 그 변형 기준으로)
- 원래 줄은 지우지 않고 status='rejected', reject_reason='→ #NN으로 옮김' (DB 상태값에 excluded 가 없어 rejected 사용)
- 한 공급처가 여러 변형을 팔면 각 품목에 따로 연결
- 맞는 변형이 없거나 판단 근거가 없으면 원래 품목에 두고 KEEP 이유를 unverified_json 에 덧붙임
- 새 품목에 같은 (공급처, 구매 방식) 줄이 이미 있으면 덮어쓰지 않음 (원래 줄만 정리)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_hub import db  # noqa: E402

from sourcing_agent.hub import rerank_top_picks  # noqa: E402

# 원래 줄 id → [(새 품목, 변형, 통화, 낮은 값, 높은 값)]  (값을 모르면 None)
MOVES = {
    # #6 냄비뚜껑·도마 거치대 → #64 부착식 2P 세트 / #65 걸이형·거치식 단품
    76: [(64, "무타공 냄비뚜껑 거치대 (접착/타공 겸용 부착식, 세트 구성 수량 미확인)", None, None, None)],
    # #16 키캡 키링 클리커 → #66 일반형(1~4구, LED 없음) / #67 LED형·9구형
    1: [(66, "논LED 디자인 키캡 키링 (강아지·고양이형 등, MOQ 50)", "USD", 0.49, 0.49),
        (67, "투명 LED 키캡 키링", "USD", 0.45, 0.69)],
    2: [(67, "발광(LED)·사운드 버튼 키보드 피젯 키링", "USD", 1.5, 1.8)],
    3: [(66, "문어 4키 클리커 (LED 없음)", "USD", 0.35, 0.65),
        (67, "LED 하트형 4키 클리커", "USD", 1.16, 1.4)],
    4: [(66, "플라스틱 키 클리커·샤프트 테스터 (LED 표기 없음, MOQ 50)", "USD", 0.4, 2.1)],
    41: [(66, "클로버형 3D프린트 키캡 키링 논LED (50개 기준)", "USD", 0.82, 0.91),
         (67, "클로버형 LED 키캡 키링 (50개 기준) / 9구 ASMR 키링 USD 1.52-1.92 (20개 기준)", "USD", 0.82, 1.92)],
    42: [(66, "아크릴·DSA 키캡 클리커 (전자부품 없음, MOQ 500)", "USD", 0.5, 0.78)],
    44: [(66, "스테인리스 금속 클릭 스위치 키링 (LED 없음)", "USD", 0.59, 1.22)],
    45: [(67, "4구 LED 키보드 키링", "USD", 1.09, 1.09)],
    55: [(67, "4구 LED 키캡 토이 키링", "USD", 1.09, 1.09)],
    56: [(67, "LED 크리스탈 키캡 키링 1~9구", "USD", 2.16, 2.16)],
    57: [(67, "4구 LED 키보드 피젯 키링", "USD", 0.99, 0.99)],
    58: [(66, "2/3/4구 핑거 키보드 키캡 키링 (LED 없음)", "USD", 1.01, 1.71)],
    59: [(66, "4구 키캡 키링 LED 없음 옵션", "USD", 0.99, 0.99),
         (67, "4구 키캡 키링 LED 옵션", "USD", 0.99, 0.99)],
    97: [(67, "4/9구 DIY 스위치 테스터 키링 (9구 기준, LED 여부 미확인)", "USD", 2.02, 2.02)],
    104: [(67, "RGB LED 키 클리커 키링 / LED 하트형 4구 USD 1.16-1.46 (MOQ 20)", "USD", 0.25, 1.46)],
    105: [(67, "전자식 키 클리커 키링 (전자부품 표기 → LED형으로 분류, 100개 이상)", "USD", 0.52, 1.12)],
    # #22·#23 AI안경 → #68 저가 번역·오디오형 / #69 카메라 탑재형(8MP·4K) / #70 정식 브랜드형
    11: [(69, "MT5 ULTRA 8MP 카메라·번역 / M02 Ultra USD 40.50-44.50", "USD", 40.5, 44.5)],
    12: [(69, "AI 번역 스마트글라스 8MP 카메라 (BT 통화·음악, 40분 녹화)", "USD", 45.2, 46.6)],
    13: [(68, "HODOLY AI 번역 안경 (CNY 112, 카메라 탑재 여부 확인 필요)", "CNY", 112, 112)],
    14: [(69, "GUHUAVMI M02S 카메라형 (HeyCyan 앱)", "USD", 86.22, 86.22)],
    15: [(69, "12MP 카메라 OEM AI 글라스 (MTK6765, MOQ 1,000~10,000)", "USD", 159, 159)],
    # Xunchitong 은 #22(ps46)·#23(ps53) 양쪽에 있음 → #69 한 줄에 두 줄의 모델을 합침 (ps53 은 '이미 있음'으로 원래 줄만 정리)
    46: [(69, "WK103 1080P 카메라 USD 21.72-22.12 (MOQ 5) / MT3 8MP USD 38 / M02 Ultra 4K USD 57.2", "USD", 21.72, 57.2)],
    48: [(69, "HG023 1080P 단일 카메라·번역 (10-99개)", "USD", 28.58, 31.71)],
    49: [(69, "ESON 4K 카메라 글라스 USD 37 / M02 듀얼 카메라 1200P USD 39.12-41.50", "USD", 37, 41.5)],
    50: [(69, "AI 번역 스마트글라스 8MP 카메라", "USD", 46.6, 46.6)],
    17: [(69, "T5 1080P 카메라 (제목 4K 표기)", "USD", 37.99, 37.99)],
    18: [(69, "GUHUAVMI M02S 카메라형 (HeyCyan 앱, 290mAh)", "USD", 86.22, 114.04)],
    19: [(69, "AI 번역 스마트글라스 8MP 카메라 (1개 소매가)", "USD", 46.6, 46.6)],
    20: [(69, "M02S 카메라형 (50% 할인가-정가)", "USD", 45.9, 91.8)],
    21: [(69, "5MP GC05A2 카메라 1080p 녹화·번역", None, None, None)],
    22: [(69, "GUHUAVMI M02 1200P 카메라형", None, None, None)],
    23: [(69, "8MP 카메라 번역안경 (모델별 46.60 / 69.77)", "USD", 46.6, 69.77)],
    51: [(69, "EG08 12MP 카메라 (디스플레이 없음) — 같은 업체 EG03 USD 33.5·EG05Ultra USD 46 은 카메라 여부 미확인",
          "USD", 56, 56)],
    53: [(69, "WK103 1080P 카메라·번역 (MOQ 5) / M02 Ultra 4K USD 57.2", "USD", 21.72, 22.12)],
    54: [(69, "Cy05 IMX219 8MP 카메라·번역 (10-99개 US$33)", "USD", 33, 35)],
    # #24 문풍지 → #73 실리콘 롤 5~6m / #74 풍지판·폼 패드·소포장
    72: [(73, "자착식 실리콘 씰링 스트립 6M 롤 (6M 단가 미확인, 1M 옵션 USD 1.09)", None, None, None)],
    73: [(73, "赛达 자착식 문틈 실리콘 씰링 6m 롤 (E·V형 규격별)", "CNY", 19.68, 36.9)],
    74: [(73, "실리콘 웨더스트립 7x10mm 미터 단위 벌크 (MOQ 10,000m, 롤 포장 아님)", "USD", 0.05, 0.05)],
    75: [(73, "5M 실리콘 갭 씰 롤 (폭 25/35/45mm)", "USD", 6.23, 6.23)],
    # #34 포켓몬 카드 → #75 일반 확장팩 박스(30팩) / #76 30주년 부스터 박스(20팩) / #78 퓨처리스틱 박스
    66: [(75, "M5 일반 확장팩 박스 (30팩)", "JPY", 7886, 9818)],
    67: [(75, "M5 어비스아이 박스 (30팩)", "USD", 70, 70)],
    68: [(76, "M6a 30th CELEBRATION 부스터 박스 (20팩)", "JPY", 21000, 42000)],
    69: [(75, "M5 어비스아이 박스 30팩 (예약판매)", "USD", 149.9, 149.9)],
    # #36 치이카와 문구·소품 → #79 소형 문구 낱개 / #80 필통·파우치 등 고가 소품·세트
    98: [(79, "씰 플레이크·샤프펜 등 소형 문구 낱개", "JPY", 550, 825)],
    99: [(79, "스티커류 낱개", "JPY", 495, 550)],
    100: [(79, "클리어 씰·마스킹테이프·포스트잇 낱개", "JPY", 440, 880)],
    102: [(80, "문구 컬렉션 '비밀의 러블리 박스' 세트 (품절)", "JPY", 3850, 3850)],
    112: [(79, "볼펜·마스코트펜 등 소형 문구 낱개", "JPY", 495, 1650)],
    113: [(79, "레터세트·클리어파일·샤프펜 낱개 (아마존 표시가)", "JPY", 650, 880)],
    114: [(79, "스티커·메모 낱개", "USD", 2.99, 5.99)],
    115: [(79, "캐릭터 문구 낱개 (도매가는 로그인 후 공개)", "JPY", 450, 750)],
    116: [(80, "케이스 포함 레터세트 USD 14.71 / 신학기 문구세트(필통 포함) USD 52.31", "USD", 14.71, 52.31)],
    117: [(80, "치이카와 문구 세트 (JAN 4901770775173)", "USD", 32.6, 34.25)],
    118: [(79, "볼펜 등 문구 낱개", "USD", 5.99, 5.99), (80, "파우치", "USD", 12, 12)],
    119: [(79, "낱개 문구 (JPY 550 수준)", "JPY", 550, 550), (80, "문구 세트 4901770728810", "USD", 35, 35)],
    120: [(79, "자·펜·스티커 등 소형 문구 낱개", "USD", 4.5, 4.9)],
    121: [(79, "메모패드·노트 등 문구 낱개", "USD", 5.99, 8.99)],
}

# 옮기지 않고 원래 품목에 두는 줄 → 이유
KEEP = {
    5: "상품 사양 정보 없음 (업체 신원도 미확인) — 일반형/LED형 판단 불가",
    43: "고양이·강아지 디자인 키링, LED 여부 확인 안 됨",
    94: "투명 케이스 클리커, LED 여부 확인 안 됨",
    95: "DIY 스위치 테스터 키링, LED 여부 확인 안 됨",
    96: "피젯 스피너형 스위치 테스터 — 나뉜 품목(1~4구·LED·9구)과 형태가 다름",
    103: "완구 공장 상품, LED 여부·구수 확인 안 됨",
    106: "의료용품 무역상, 상품 사양 정보 없음",
    47: "카메라 탑재 여부 확인 안 됨 (모델 미확인)",
    16: "W AI3 카메라 탑재 여부 확인 안 됨",
    52: "카메라 탑재 여부 확인 안 됨 (USD 19~21, 저가 번역형일 가능성)",
    24: "'Lenovo' 명칭 상품 — 정식 브랜드 정품인지 불명 (8K 표기 과장 의심)",
    25: "'Lenovo' 명칭 상품, 평점 1.0·사양 불일치 — 정품 여부 불명",
    71: "미터 단위 고무·플라스틱 씰 — 재질(실리콘 여부)·롤 포장 미확인",
    70: "구 시리즈 박스(로켓단의 영광·블랙볼트 20팩) — 나뉜 품목(메가 30팩·30주년·퓨처리스틱)과 다름",
    101: "취급 상품·가격 정보 없음 — 낱개/고가 소품 판단 불가",
}

FX = {"USD": 1.0, "CNY": 7.2, "JPY": 154.0}   # price_basis.fx_assumed (원래 조사 때 쓴 가정과 같은 수준)
COPY = ("moq", "purchase_mode", "max_order_qty", "score", "grade", "score_detail_json", "first_questions_json",
        "unverified_json", "evidence_json", "specs_json", "image_url")


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = db.connect()
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM product_suppliers WHERE id IN (%s)" % ",".join(
        str(i) for i in [*MOVES, *KEEP]))}
    missing = [i for i in [*MOVES, *KEEP] if i not in rows]
    if missing:
        sys.exit(f"없는 줄: {missing}")
    moved, kept, skipped, touched = 0, 0, [], set()
    with conn:
        for ps, targets in MOVES.items():
            r = rows[ps]
            terms = _j(r["trade_terms_json"], {}) or {}
            dest = []
            for pid, variant, cur, low, high in targets:
                exists = conn.execute("SELECT id FROM product_suppliers WHERE product_id = ? AND supplier_id = ?"
                                      " AND purchase_mode IS ?", (pid, r["supplier_id"], r["purchase_mode"])).fetchone()
                dest.append(pid)
                if exists:
                    skipped.append(f"ps{ps} → #{pid}: 같은 공급처·구매 방식 줄(ps{exists['id']})이 이미 있어 그대로 둠")
                    continue
                basis = {k: v for k, v in (terms.get("price_basis") or {}).items() if k not in ("low", "high", "tiers")}
                basis["variant"] = variant
                usd = None
                if cur and low:
                    basis.update(currency=cur, low=low, high=high or low)
                    if cur != "USD":
                        basis["fx_assumed"] = FX[cur]
                    usd = round((high or low) / FX[cur], 2)
                basis["evidence"] = (basis.get("evidence") or []) + [
                    {"claim": f"#{r['product_id']}(변형 혼합) 공급처 줄 ps{ps}에서 옮김 — 변형: {variant}", "url": None}]
                new_terms = dict(terms, price_basis=basis)
                link = {k: r[k] for k in COPY if r[k] is not None}
                rng = f"{cur} {low:g}-{(high or low):g}" if cur and low else None
                link.update(trade_terms_json=new_terms, is_top_pick=0,
                            price_note=" — ".join(x for x in [rng, variant] if x))
                if usd:
                    link["unit_price_usd"] = usd
                print(f"  ps{ps} #{r['product_id']} → #{pid}  {rng or '단가 미확인'} — {variant}")
                if a.apply:
                    db.link_supplier(conn, pid, r["supplier_id"], r["run_id"], **link)
                touched.add((pid, r["purchase_mode"]))
                moved += 1
            reason = "→ " + ", ".join(f"#{p}" for p in dict.fromkeys(dest)) + "으로 옮김"
            if a.apply:
                conn.execute("UPDATE product_suppliers SET status = 'rejected', reject_reason = ?, is_top_pick = 0,"
                             " updated_at = ? WHERE id = ?", (reason, db.now(), ps))
        for ps, why in KEEP.items():
            r = rows[ps]
            print(f"  ps{ps} #{r['product_id']} 그대로 — {why}")
            if a.apply:
                unv = _j(r["unverified_json"], []) or []
                note = f"나뉜 품목으로 옮기지 않음: {why}"
                if note not in unv:
                    conn.execute("UPDATE product_suppliers SET unverified_json = ?, updated_at = ? WHERE id = ?",
                                 (json.dumps(unv + [note], ensure_ascii=False), db.now(), ps))
            kept += 1
        if a.apply:
            for pid, mode in sorted(touched, key=lambda x: (x[0], str(x[1]))):
                rerank_top_picks(conn, pid, mode)
            for pid in sorted({p for p, _ in touched}):
                n = sum(1 for p, _ in touched if p == pid)
                stage = conn.execute("SELECT stage FROM products WHERE id = ?", (pid,)).fetchone()["stage"]
                conn.execute("INSERT INTO product_events (product_id, from_stage, to_stage, note) VALUES (?, ?, ?, ?)",
                             (pid, stage, stage, "변형 혼합 품목의 공급처 줄을 이 품목으로 옮김 (2번)"))
    for s in skipped:
        print("  ·", s)
    print(f"{'반영' if a.apply else '미리보기'}: 새 연결 {moved}건 · 원래 품목에 둠 {kept}줄 · 이미 있어 건너뜀 {len(skipped)}건")


if __name__ == "__main__":
    main()
