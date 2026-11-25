# 공급처 발굴 에이전트 (trade-sourcing-agent)

1인 무역용 공급처 발굴 에이전트입니다. 품목 조건을 넣으면 후보 업체를 찾고 신뢰도를 검증해서, 점수순 비교표와 추천 리포트를 만들어 줍니다.

1인 수입 무역 파이프라인(1 시장조사 → **2 공급처 발굴** (2-R 구매대행) → 3 통관·규제 체크 → 4 원가·수익 계산 → 5 커뮤니케이션·협상 → 6 무역서류 → 7 판매 → 8 재무) 중 **2단계: 공급처 발굴**을 맡습니다.
구매대행(retail) 조사 결과는 2-R 구매대행 모니터(buyagent)로 넘깁니다.
현재는 수입용 공급처 발굴(`mode: supplier`)만 지원하고, 수출용 바이어 발굴(`mode: buyer`)은 추후 추가 예정입니다.

## 동작 방식 (하이브리드)

알리바바·1688은 크롤링을 막고 있고 약관으로도 금지하고 있어서, 페이지를 직접 긁지 않습니다.

| 단계 | 방식 | 자동/수동 |
|---|---|---|
| ② 검색 | Claude API 웹 검색으로 검색엔진 결과에서 후보 수집 (`site:alibaba.com` 등) | 자동 |
| ②' 마켓 API | 중국은 AliExpress, 미국은 eBay **공식 API**로 실제 판매가·판매 수·판매자 평점·포장 무게 확인 (무료, 키 없으면 건너뜀) → 검색·검증 AI 의 근거, 같은 상품 업체에 포장·무게 채움 | 자동 |
| ③ 1차 필터 | 제조사 여부, 업력, 인증, 홈페이지 유무로 규칙 기반 선별 (LLM 비용 없음) | 자동 |
| ③' 수동 보충 | 로그인해야 보이는 정보를 `data/manual/업체명.txt` 에 붙여넣기 | 수동 (선택) |
| ④ 검증 | 홈페이지, 기업정보, 인증, 사기 이력 교차 확인 후 점수표로 채점 | 자동 |
| ⑤ 리포트 | `results.csv`(엑셀), `report.md`, `results.json` | 자동 |

점수 합산과 등급은 LLM이 아니라 코드(`scoring.py`)가 계산해서, 기준이 매번 똑같이 적용됩니다.

마켓 API 키(`ALIEXPRESS_APP_KEY`·`ALIEXPRESS_APP_SECRET`, `EBAY_CLIENT_ID`·`EBAY_CLIENT_SECRET`)는 `.env` 에 넣습니다. AliExpress 로그인 토큰은 buyagent 가 발급·갱신하는 `buyagent/data/aliexpress_token.json` 을 **읽기만** 합니다 (여러 곳에서 갱신하면 서로의 토큰이 무효가 됨). 토큰이 만료됐으면 buyagent 를 한 번 실행하면 갱신됩니다. 일본은 아직 마켓 API 가 없어 웹 검색만 씁니다. 요청에 `market_api: false` 를 넣으면 이 단계를 끕니다.

소량 사입(small_batch)은 목표 단가를 1번의 FOB(수백 개 이상 공장 단가) 대신 **소량 구매 단가 상한**으로 봅니다: 국내 판매가 ÷ 1.1 × (1 − 수수료 − 목표 마진) ÷ 환율 ÷ (1+관세) − 개당 운임 (값은 `config/scoring/small_batch.yaml` 의 `price_ceiling`). FOB 는 참고값으로만 넘기고, 대량 사입·OEM 은 지금처럼 FOB 기준입니다. 또 소량 사입 후보에는 소매 마켓 판매자(AliExpress·eBay 상품 페이지만 있는 곳)를 최대 2곳만 넣고 나머지는 1688·Alibaba 도매 후보로 채웁니다.

1번이 판매가가 다른 변형을 별도 품목으로 나눈 원래 품목(`products.notes` 가 `[변형 혼합]` 으로 시작)은 판매가가 섞여 단가 상한이 맞지 않으므로 조사하지 않고, notes 의 나눈 품목 번호(#64 등)를 안내합니다.

## 설치 (Python 3.10 이상)

```bash
cd trade_sourcing_agent
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -e .                  # 또는 pip install -r requirements.txt

cp .env.example .env              # Windows: copy .env.example .env
# .env 를 열어 ANTHROPIC_API_KEY 입력 (실제 키는 .env 에만! .env.example 은 git 에 올라갑니다)
```

`pip install -e .` 는 반드시 `-e`(편집 모드)로 설치하세요. `prompts/`, `config/`, `output/` 을 프로젝트 폴더에서 읽기 때문입니다.

## 사용법

1. 요청 파일을 복사해서 품목 조건을 채웁니다.
   - 소매 직구(낱개~10개, 시장 반응 보기): `sourcing_type: retail` + `max_order_qty: 10`
   - 소량 도매(테스트 판매): `requests/keyboard_small_batch.yaml` (`sourcing_type: small_batch`, `max_order_qty`)
   - 대량 사입·OEM(공장·대형 도매 직거래): `requests/example.yaml` (`sourcing_type` 생략 시 `oem`)

   | 구매 방식 | 언제 | 주로 찾는 곳 | 점수 중점 |
   |---|---|---|---|
   | `retail` | 몇 개만 사서 반응 보기 | AliExpress·Taobao·Tmall 스토어 | 판매자 평판, 상품 후기, 한국 배송, 정품 여부 |
   | `small_batch` | 50개 이하 테스트 판매 | Alibaba Ready to Ship, 1688, AliExpress 도매 | 소량 구매 가능, 평판, 시험성적서 |
   | `oem` (대량 사입·OEM) | 판매가 검증된 뒤 대량 구매(기성품) 또는 맞춤 제작 | 제조 공장, 대형 도매 | 제조사 여부, 인증, MOQ·수량 구간 단가, 실적 (OEM 대응은 가점) |

2-A. **한 번에 실행** (수동 정보 없이)
```bash
python -m sourcing_agent run requests/example.yaml
```

2-B. **수동 정보를 넣어서 실행** (추천)
```bash
python -m sourcing_agent discover requests/example.yaml
# → data/manual/ 에 후보별 .txt 파일이 생깁니다.
#   알리바바 상세페이지 회사정보, 채팅 답변 등을 복사해서 붙여넣으세요.
python -m sourcing_agent verify output/<실행폴더>
```

3. 결과는 `output/<날짜_품목>/` 폴더에 있습니다.
   - `results.csv`: 엑셀로 여는 비교표
   - `report.md`: 추천 TOP 3, 근거 링크, 첫 문의 때 물어볼 질문

## 공용 DB(trade_hub)에서 품목 받아 실행하기 (추천)

1번 에이전트(시장조사)가 찾은 후보 중 사람이 고른 품목을 DB에서 바로 읽어 처리합니다. 요청 YAML을 만들 필요가 없습니다.

```bash
pip install -e ../trade_hub                      # 처음 한 번 (requirements.txt에 포함)
trade_hub select 16 17                           # 사람이 진행할 품목을 고름 (stage → sourcing)
python -m sourcing_agent from-db --dry-run       # DB에서 만든 소싱 조건만 확인 (API 호출 없음)
python -m sourcing_agent from-db                 # 아직 조사하지 않은 sourcing 품목 전부 처리
python -m sourcing_agent from-db 17 --type retail                # 소매 직구 (기본 10개 이하)
python -m sourcing_agent from-db 17 --type oem --max-qty 100   # 특정 품목만, 조건 지정
```

- 구매 방식 기본값은 `small_batch`(50개 이하)입니다. `--max-qty`를 생략하면 `retail`은 10개, `oem`은 수량 제한 없음.
- `--country Japan` 이면 일본 공급처를 찾습니다 (아래 "일본 공급처 발굴"). 기본은 `China`.
- `--type retail` 이면 조사가 끝난 뒤 결과를 구매대행 모니터(buyagent)에 자동으로 추가합니다 (아래 "구매대행으로 팔기", 끄려면 `--no-buyagent`).

- 소싱 조건은 DB의 품목명·영어 검색어·FOB/판매가 시세·선정 이유·예상 규제·위험 요소로 채웁니다.
  인증 확정은 3번(통관·규제 체크) 단계 일이라 `required_certs`는 비우고 메모로만 전달합니다.
- 결과는 `output/` 리포트와 함께 공용 DB의 `suppliers`·`product_suppliers`(상위 3곳 추천 표시, 구매 방식 `purchase_mode`)와
  `product_channels`(구매 사이트 순위)에 기록되고,
  `tasks`에 "공급처 검토" 할 일이 생깁니다. 품목 단계(stage)는 바꾸지 않습니다.
- 한 번 조사한 품목은 공급처를 못 찾았어도 자동으로 다시 돌리지 않습니다. 다시 하려면 번호를 지정하세요.
- 공용 DB 위치는 환경변수 `TRADE_DB`(없으면 `../trade_hub/data/trade.db`).

## 일본 공급처 발굴

```bash
python -m sourcing_agent from-db 23 --type retail --country Japan        # 일본 구매대행용
python -m sourcing_agent from-db 23 --type small_batch --country Japan   # 일본 소량 사입용
```

- 조사 조건의 `preferred_countries` 가 `Japan` 이 되고, 일본어 검색어·엔화 원문+USD 환산·해외(한국) 거래 가능 여부 확인을 지시합니다.
  요청 YAML 로 실행할 때는 `preferred_countries: [Japan]` 을 쓰면 됩니다.
- 일본 사이트 API 는 쓰지 않습니다. 조사는 Claude 웹 검색, 사이트 검색 링크는 코드가 만듭니다.
- 업체 검증: 국세청 법인번호 공표 사이트, 「特定商取引法に基づく表記」(사업자 정보 표기), 「会社概要」로 실존·주소 확인.
- 구매 사이트 순위 (일본):

  | 구매 방식 | 사이트 |
  |---|---|
  | `retail` | 라쿠텐 · Amazon.co.jp · Yahoo!ショッピング |
  | `small_batch` | 라쿠텐 · Amazon.co.jp · NETSEA · SUPER DELIVERY(해외 바이어용 영문 사이트) |
  | `oem` | NETSEA · SUPER DELIVERY |

- 일본 쇼핑몰은 대부분 해외 배송이 안 돼 배송대행지가 필요하고, 도매몰은 일본 사업자만 거래하는 판매자가 많습니다.
  그래서 사이트 고유 점수의 '한국 배송'이 낮게 잡혀 있습니다 (`config/channels.yaml`).
- **해외 직배송 판매자 우선**: 「海外発送」「海外配送 対応」「international shipping」 검색, 메이커 공식몰의 해외 판매 페이지,
  Amazon.co.jp 해외 발송 상품 등을 먼저 찾고 후보의 절반 이상을 해외 직배송 판매자로 채우게 합니다.
  확인 결과는 `trade_terms.ships_to_korea`(true / false / null)·`korea_shipping` 에 남고,
  소매·소량 사입이면 1차 필터에서 직배송 +3 / 국내배송만 −1 (빼지는 않음), 점수표의 한국 배송 항목도 직배송이 만점입니다.
  리포트·CSV 에는 '한국 배송' 줄로 나옵니다.
- 구매대행(retail)은 buyagent 가 라쿠텐·Yahoo!쇼핑 최저가를 직접 감시하므로, 2번은 정품 판매처·공식몰 확인 위주로 쓰면 됩니다.

## 구매 사이트 순위

공급처(업체)와 별도로, 이 품목을 **어느 사이트에서 사는 게 좋은지** 점수와 검색 링크로 보여 줍니다
(리포트 상단 "구매 사이트 순위", `results.json` 의 `channels`).

- 점수 = 항목별 평가(0~5) ÷ 5 × 가중치, 합계 100점
  - 품목별(웹 검색으로 매번 평가): 소량 구매 가능 · 가격 · 상품 선택지
  - 사이트 고유(설정 파일 고정값): 구매자 보호 · 한국 배송 · 거래 편의 · 서류 대응
  - 가중치는 구매 방식별로 다름 (소매는 보호·배송, 소량 도매는 가격·서류 비중이 큼)
- 대상 사이트도 구매 방식별로 다름: retail(AliExpress·Alibaba·1688·Taobao·DHgate), small_batch(AliExpress·Alibaba·1688·DHgate·Made-in-China), oem(Alibaba·1688·Made-in-China·Global Sources)
- 검색 링크는 코드가 만들어서 항상 열립니다. 1688·Taobao 는 중문 검색어를 씁니다.
- 실행마다 API 호출 1회(+검색 최대 `MAX_SEARCHES_CHANNELS`, 기본 8회)가 추가됩니다. 끄려면 요청에 `assess_channels: false`.
- 이미 조사한 결과에 추가: `python -m sourcing_agent channels output/<실행폴더>`

## 구매대행으로 팔기: 구매대행 모니터(buyagent)로 내보내기

재고 없이 주문이 들어올 때마다 해외 상품 페이지에서 1개씩 사서 고객 명의로 보내는 경우,
조사 결과의 **상품 상세 페이지 링크와 USD 단가**를 buyagent 카탈로그로 넘겨서 원가·권장가·마진 계산과 가격 감시를 맡깁니다.

```bash
python -m sourcing_agent export-buyagent output/<실행폴더>            # 미리보기 (추가 N줄 / 갱신 N줄 / 그대로 N줄)
python -m sourcing_agent export-buyagent output/<실행폴더> --write    # buyagent/catalog 에 쓰기
cd ../buyagent/buyagent && python main.py --only <product_id>         # 원가·권장가 확인
```

- **retail(구매대행) 조사 결과만** 넘깁니다. 소량 사입·OEM 결과는 도매가라 거절합니다
  (꼭 필요하면 `--allow-non-retail`, 이때 memo 에 "소량 사입 결과(도매가)" 표시).
- 판매처는 **낱개 구매가 되는 곳만** (MOQ 첫 숫자가 2 이상이면 제외), 상품 상세 페이지와 USD 단가가 있어야 합니다.
- **가품·상표 도용 위험이 지적된 판매처**(검증 risk_flags 에 '도용·가품·위조' 등)와 **25점 미만** 판매처는 제외합니다.
  buyagent 는 가장 싼 판매처를 고르므로, 싸고 위험한 곳이 1순위가 되는 것을 막기 위함입니다 (`--min-score`, 환경변수 `BUYAGENT_MIN_SCORE`).
- `products.csv` 에 품목 1줄(`sources=manual`, `hub_product_id` = 공용 DB 품목 번호), `manual_prices.csv` 에 판매처마다 1줄.
  buyagent 에 `hub_product_id` 열이 없으면 기존 열만 쓰고 경고합니다.
- 같은 (품목 id, 링크) 가 이미 있으면 가격·배송비·재고·확인 날짜·memo 를 새 값으로 **갱신**합니다 (줄은 늘지 않음).
  memo 에 "2번 공급처 발굴에서 가져옴" 이 있는 줄만 갱신하고, 사람이 손으로 쓴 줄은 건드리지 않습니다.
- 현지 배송비는 모르면 빈 칸(buyagent 에서 "미확인"), 무료배송이 확실할 때만 `0`.
- `from-db --type retail` 은 조사 후 이 내보내기를 자동으로 합니다. 실패해도 조사 결과·DB 저장은 그대로입니다.
- 카테고리는 품목명으로 추정합니다 (AI 안경 → `smart_wearable`, 키캡 → `keycap` 등, 목록은 `buyagent_export.py` 의 `CATEGORY_HINTS`).
  봉제인형·유아용품·일반 전자제품처럼 buyagent 에서 구매대행 금지인 카테고리는 내보내지 않고 사유를 알려 줍니다. 직접 정하려면 `--category`.
- 넘길 판매처가 하나도 없으면 품목도 추가하지 않습니다.
- buyagent 위치: `--buyagent` 또는 환경변수 `BUYAGENT_DIR` (기본 `../buyagent/buyagent`).

## 다른 에이전트에서 호출하기

### Python (같은 venv 에 `pip install -e /경로/trade_sourcing_agent` 후)

```python
from sourcing_agent import find_suppliers, run_discover, run_verify

result = find_suppliers({
    "product": "스테인리스 텀블러 500ml",
    "product_en": "stainless steel tumbler 500ml",
    "target_price": "USD 2.5 이하 (FOB)",
    "moq": "1,000개 이하",
    "required_certs": ["FDA", "LFGB"],
    "preferred_countries": ["China"],
    "prefer_manufacturer": True,
    "min_candidates": 20,
    "shortlist_size": 10,
})
print(result["top3"])           # 상위 3곳 요약 (연락처 포함)
print(result["files"]["json"])  # 다음 단계로 넘길 results.json 경로
```

진행 로그는 stderr 로 나갑니다. 로그를 받고 싶으면 `find_suppliers(req, log=내함수)` 로 넘기세요.

### CLI (다른 언어·도구로 만든 에이전트)

```bash
python -m sourcing_agent run requests/example.yaml > out.json
```

- **stdout 에는 결과 JSON 만** 나옵니다 (진행 로그·안내는 stderr).
- 실패하면 종료 코드 1, stdout 에 `{"error": {"type": ..., "message": ...}}`.
- 결과 JSON 의 `files.json` 경로에서 `results.json` 을 읽으면 됩니다.

## 출력 형식: `results.json` (다음 단계 에이전트용)

```jsonc
{
  "schema_version": "1.2",          // 1.x = 필드 추가만(하위 호환), 2.0 = 깨지는 변경
  "agent": "trade_sourcing_agent",
  "mode": "supplier",
  "sourcing_type": "small_batch",   // "retail" / "small_batch" / "oem" — scores 의 항목 key 가 방식별로 다름
  "generated_at": "2026-11-16T14:00:00+09:00",
  "request": { "product": "...", "hs_code": "9617.00", "target_price": "...", ... },
  "suppliers": [                    // 총점 내림차순
    {
      "rank": 1, "name": "...", "country": "China", "company_type": "manufacturer",
      "urls": ["..."], "website": "...", "certifications": ["FDA"], "moq": "...", "price_range": "...",
      "contacts":    { "contact_person", "email", "phone", "messenger", "address" },
      "trade_terms": { "lead_time", "payment_terms", "sample", "export_markets",
                       "ships_to_korea",   // true 한국 직배송 / false 국내배송만(배송대행지 필요) / null 모름
                       "korea_shipping" }, // 방법·배송비·기간 (예: "EMS 2,000엔, 7~10일")
      "scores": { ... }, "risk_penalty": 0, "total": 72.0, "grade": "B",
      "risk_flags": [], "unverified": [], "evidence": [{ "claim", "url" }],
      "summary": "...", "first_questions": ["..."], "manual_info_used": false,
      "specs": {                     // 제품 사양 (1.1~, 상품 페이지에서 확인한 사실만, 모르면 null). 공용 DB product_suppliers.specs_json
        "flags": {"power_source": "battery_lithium", "is_electronic": true, "wireless": true, "food_contact": null, "...": null},
        "materials": ["ABS"], "evidence": [{"claim": "리튬 배터리 내장", "url": "https://..."}]
      },
      "packaging": {                 // 포장·무게 (1.2~, 페이지에서 확인한 값만, 모르면 null). 공용 DB trade_terms_json.packaging
        "unit_weight_kg": 0.35, "unit_size_cm": [30, 10, 5], "pcs_per_carton": 50,
        "carton_weight_kg": 18.5, "carton_size_cm": [60, 40, 35],
        "per_unit_kg": 0.37,         // 카톤 총무게 ÷ 카톤당 수량 (없으면 개당 무게)
        "per_unit_cbm": 0.00168,     // 카톤 부피 ÷ 카톤당 수량 (없으면 개별 포장 부피), ㎥
        "evidence": [{"claim": "Gross weight 0.35kg, 50pcs/carton", "url": "https://..."}]
      },
      "model": "AULA F75",           // 브랜드+모델 (1.5~, 상품 페이지에서 확인, 무브랜드면 null)
      "quality": {                   // 제품 품질·리뷰 (1.5~, 모델별 1회 조사). 공용 DB product_suppliers.quality_json (칸이 있을 때)
        "model": "AULA F75", "review_sources": 6, "review_count_total": 3400,
        "pros": [{"point": "타건감 좋음", "url": "https://..."}], "cons": [{"point": "2.4G 연결 끊김", "url": "https://..."}],
        "spec_doubts": [{"spec": "배터리", "claimed": "4000mAh", "observed": "3~4일", "status": "exaggerated", "url": "..."}],
        "competitors": [...], "attributes": [{"name": "연결 방식", "ours": "…", "competitor": "…", "verdict": "우위"}],
        "vs_competitors": "…", "verdict": "good",   // good / ok / weak / unknown (리뷰 부족 → 샘플 확인 필요)
        "sample_needed": false, "note": null, "evidence": [{"claim", "url"}], "checked_at": "2026-11-25"
      },                             // evidence_json 에도 {"claim","url","type":"quality"} 요약·불만·과장 의심 항목
      "price_basis": {               // 가격 기준 (1.4~, 확인한 키만 — 모르는 키는 없음). 공용 DB trade_terms_json.price_basis
        "currency": "JPY", "low": 21000, "high": 42000, "fx_assumed": 154,
        "includes_shipping": false,  // 한국까지 배송비 포함 가격이면 true → 4번이 운임을 0으로
        "includes_tax": true,        // 한국 관부가세까지 포함(DDP)일 때만 키가 있음 (판매국 소비세 포함은 해당 없음)
        "preorder": true,
        "kind": "retail",            // factory / wholesale / retail / resale (AliExpress·eBay 상품 페이지뿐이면 retail)
        "variant": "4구 LED형",      // 이 단가가 가리키는 상품. DB price_note 에도 "USD … — 4구 LED형" 으로 붙임
        "tiers": [{"min_qty": 2, "price": 13.9}, {"min_qty": 100, "price": 11.5}],   // 수량 구간별 단가 (currency 통화) → 4번 최적 주문 수량
        "evidence": [{"claim": "JPY 2,980 税込 送料別", "url": "https://..."}]
      }
    }
  ]
}
```

모르는 값은 `null` 입니다. 3번 통관·규제 체크는 `request.hs_code` + `specs`, 4번 원가·수익 계산은 `unit_price_usd`(범위의 높은 쪽 USD) + `packaging`(운임 추정) + `price_basis`(배송비 포함 여부·도매/소매·변형), 5번 협상은 `contacts` + `first_questions` 를 쓰면 됩니다.

## 커스터마이즈

| 바꾸고 싶은 것 | 파일 |
|---|---|
| 구매 방식 선택 | 요청 YAML 의 `sourcing_type` (`retail` 소매 직구 / `small_batch` 소량 도매 / `oem` 대량 사입·OEM, 기본 `oem`) |
| 점수 기준, 가중치, 등급, 방식별 검색·검증 방향 | `config/scoring/<sourcing_type>.yaml` |
| 구매 사이트 목록·검색 링크·사이트 고유 점수·방식별 가중치 | `config/channels.yaml` (평가 지시문: `prompts/channels.md`) |
| 검색 전략, 수집 항목 | `prompts/discover.md` |
| 검증 항목, 질문 생성 | `prompts/verify.md` |
| 모델, 검색 횟수 (비용) | `.env` |

## 비용 참고

검증은 후보 1곳당 API 호출 1회(+웹 검색 여러 번)가 들어갑니다. 처음에는 `shortlist_size: 3~5`, `MAX_SEARCHES_VERIFY=6` 정도로 작게 돌려 보세요.

## 주의

- AI 검증은 1차 거르기용입니다. 거래 전에는 샘플 주문, 화상 공장 투어, 제3자 실사(SGS·QIMA 등)로 최종 확인하세요.
- 웹 검색 툴 버전(`WEB_SEARCH_TOOL`)과 모델명은 Anthropic 문서(docs.claude.com)에서 최신 값을 확인하세요.
