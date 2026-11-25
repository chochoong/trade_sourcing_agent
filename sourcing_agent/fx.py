"""환율 — 다시 찾기 단가 상한(원화)을 공급 국가 통화·USD 로 바꿀 때 씀.

4번 cost_agent·5번과 같은 공개 환율 API(open.er-api.com, 통화 코드만 보냄). 못 가져오면 예비값 + '예비 환율' 표시.
"""
import json
import urllib.request

FX_URL = "https://open.er-api.com/v6/latest/USD"
# 1 USD = ? 통화 (API 실패 시 예비값 — 대략값이므로 결과에 '예비 환율'로 표시)
FALLBACK_PER_USD = {"USD": 1.0, "KRW": 1400.0, "CNY": 7.2, "JPY": 150.0, "EUR": 0.92}
COUNTRY_CURRENCY = {"China": "CNY", "Japan": "JPY", "USA": "USD"}
_cache: dict = {}


def per_usd():
    """({통화: 1 USD 당 금액}, 출처 설명). 한 프로세스에서 한 번만 조회."""
    if "rates" not in _cache:
        try:
            with urllib.request.urlopen(FX_URL, timeout=8) as r:
                data = json.loads(r.read().decode())
            rates = {k: float(v) for k, v in (data.get("rates") or {}).items() if isinstance(v, (int, float)) and v > 0}
            if not 500 < rates.get("KRW", 0) < 5000:
                raise ValueError("KRW 환율 이상")
            _cache["rates"] = rates
            _cache["source"] = f"open.er-api.com {(data.get('time_last_update_utc') or '')[5:16]} 기준".strip()
        except Exception:
            _cache["rates"] = dict(FALLBACK_PER_USD)
            _cache["source"] = "예비 환율 (실시간 조회 실패 — 대략값)"
    return _cache["rates"], _cache["source"]


def krw_to(krw, currency):
    """원화 → currency 금액. (금액, 1 currency = ?원, 출처). 환율이 없으면 USD 로."""
    rates, source = per_usd()
    cur = currency if currency in rates else "USD"
    krw_per_unit = rates["KRW"] / rates[cur]
    return krw / krw_per_unit, cur, krw_per_unit, source


def usd_to_krw(usd):
    rates, _ = per_usd()
    return usd * rates["KRW"]
