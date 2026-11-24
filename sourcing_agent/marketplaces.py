"""② 검색 전: 공식 마켓 API로 실제 판매 상품 확인 (AliExpress = 중국, eBay = 미국). 읽기 전용, 무료.

- AliExpress Open Platform 드롭쉬핑 앱: aliexpress.ds.text.search(키워드) → aliexpress.ds.product.get(상품 상세)
  상세에서 상점 이름·평점, 판매 수, 포장 크기·총무게(→ 4번 운임 추정), 재질·원산지를 받음.
  로그인 토큰은 buyagent 가 발급·갱신하는 파일을 **읽기만** 함 (여러 곳에서 갱신하면 서로의 토큰이 무효가 됨).
- eBay Browse API: Application 토큰(사용자 로그인 없음) → item_summary/search → item/{id}
  판매자 피드백, 판매 수량, 재질·원산지를 받음.

결과(snapshot)는 소싱 조건 request["market_listings"] 로 들어가 검색·검증 단계의 AI가 근거로 쓰고,
검증 후 같은 상품 페이지의 업체에는 API 값(포장·무게)을 그대로 채움.
키·토큰이 없거나 API 오류면 MarketError → 이 단계만 건너뛰고 조사는 평소대로.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .packaging import normalize_packaging

ALI_GATEWAY = "https://api-sg.aliexpress.com/sync"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_API = "https://api.ebay.com/buy/browse/v1"
TIMEOUT = 20
SEARCH_N = 20      # 검색 결과 수 (가격대 계산용)
DETAIL_N = 5       # 상세 조회할 상품 수 (판매량 상위)


class MarketError(RuntimeError):
    pass


def _num(v, cast=float):
    try:
        return cast(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _http(req):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise MarketError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MarketError("접속 실패 (인터넷 연결 확인)") from None
    except ValueError:
        raise MarketError("응답 형식 오류") from None


# ---------------------------------------------------------------- AliExpress
def ali_token_file():
    if os.environ.get("ALIEXPRESS_TOKEN_FILE"):
        return Path(os.environ["ALIEXPRESS_TOKEN_FILE"])
    from .buyagent_export import DEFAULT_BUYAGENT
    return Path(DEFAULT_BUYAGENT) / "data" / "aliexpress_token.json"


def _ali_auth():
    key = os.environ.get("ALIEXPRESS_APP_KEY", "").strip()
    secret = os.environ.get("ALIEXPRESS_APP_SECRET", "").strip()
    if not key or not secret:
        raise MarketError(".env 에 ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET 없음")
    path = ali_token_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MarketError("로그인 토큰 파일 없음 — buyagent 에서 AliExpress 토큰 발급 필요") from None
    except (OSError, ValueError):
        raise MarketError("로그인 토큰 파일을 읽지 못함") from None
    tok = str(data.get("access_token") or "").strip()
    if not tok:
        raise MarketError("로그인 토큰 파일에 access_token 없음")
    expire = _num(data.get("expire_time")) or 0
    if expire and time.time() * 1000 >= expire - 60_000:
        raise MarketError("로그인 토큰 만료 — buyagent 를 한 번 실행하면 자동 갱신됨")
    return key, secret, tok


def ali_sign(params, secret):
    """IOP 서명: 이름순 '이름+값' 이어 붙여 HMAC-SHA256(App Secret) → 대문자 16진수."""
    text = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


def ali_call(method, params):
    key, secret, tok = _ali_auth()
    p = {"method": method, "app_key": key, "sign_method": "sha256", "timestamp": str(int(time.time() * 1000)),
         "session": tok, **{k: str(v) for k, v in params.items()}}
    p["sign"] = ali_sign(p, secret)
    data = _http(urllib.request.Request(ALI_GATEWAY, data=urllib.parse.urlencode(p).encode(),
                                        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}))
    if "error_response" in data:
        e = data["error_response"] or {}
        raise MarketError(f"API 오류 {e.get('code', '')} {e.get('msg') or e.get('sub_msg') or ''}".strip())
    return data


def ali_item_url(raw):
    if not raw:
        return None
    url = ("https:" + raw) if raw.startswith("//") else raw
    return url.split("?", 1)[0]


def parse_ali_search(data):
    body = data.get("aliexpress_ds_text_search_response") or {}
    if str(body.get("code", "00")) not in ("00", "0"):
        raise MarketError(f"검색 오류 {body.get('code')} {body.get('msg') or ''}".strip())
    d = body.get("data") or {}
    items = []
    for it in ((d.get("products") or {}).get("selection_search_product")) or []:
        price, url = _num(it.get("targetSalePrice")), ali_item_url(it.get("itemUrl"))
        if price and url:
            items.append({"id": str(it.get("itemId") or ""), "title": str(it.get("title") or "")[:100],
                          "url": url, "price_usd": price, "sold": _num(it.get("orders"), int) or 0,
                          "rating": _num(it.get("score"))})
    return _num(d.get("totalCount"), int) or len(items), items


def _ali_props(res):
    raw = res.get("ae_item_properties") or {}
    raw = raw.get("ae_item_property") if isinstance(raw, dict) else raw
    out = {}
    for p in raw or []:
        if isinstance(p, dict) and p.get("attr_name") and p.get("attr_value"):
            out.setdefault(str(p["attr_name"]).strip(), str(p["attr_value"]).strip())
    return out


def parse_ali_detail(data, url=None):
    """ds.product.get → 상점·판매·포장·재질·원산지. 받은 값만."""
    res = (data.get("aliexpress_ds_product_get_response") or {}).get("result") or {}
    store, base, pkg = res.get("ae_store_info") or {}, res.get("ae_item_base_info_dto") or {}, \
        res.get("package_info_dto") or {}
    props = _ali_props(res)
    out = {}
    if store.get("store_name"):
        out["store"] = {"name": store["store_name"], "id": store.get("store_id"),
                        "country": store.get("store_country_code"),
                        "rating_described": _num(store.get("item_as_described_rating")),
                        "rating_communication": _num(store.get("communication_rating")),
                        "rating_shipping": _num(store.get("shipping_speed_rating"))}
    if _num(base.get("sales_count"), int) is not None:
        out["sold"] = _num(base.get("sales_count"), int)
    if _num(base.get("evaluation_count"), int):
        out["reviews"] = _num(base.get("evaluation_count"), int)
        out["rating"] = _num(base.get("avg_evaluation_rating"))
    dims = [_num(pkg.get(k)) for k in ("package_length", "package_width", "package_height")]
    gw = _num(pkg.get("gross_weight"))
    claim = "AliExpress API 포장 정보: " + ", ".join(
        x for x in [f"총무게 {gw:g}kg" if gw else "", "크기 " + "×".join(f"{d:g}" for d in dims) + "cm"
                    if all(dims) else ""] if x)
    packaging = normalize_packaging({"unit_weight_kg": gw, "unit_size_cm": dims if all(dims) else None,
                                     "evidence": [{"claim": claim, "url": url}]})
    if packaging:
        out["packaging"] = packaging
    for key, names in (("material", ("Material", "material")), ("origin", ("Origin", "Place of Origin"))):
        v = next((props[n] for n in names if props.get(n)), None)
        if v:
            out[key] = v
    return out


def ali_snapshot(keyword):
    data = ali_call("aliexpress.ds.text.search", {"keyWord": keyword, "local": "en_US", "countryCode": "KR",
                                                  "currency": "USD", "pageSize": SEARCH_N, "pageIndex": 1})
    total, items = parse_ali_search(data)
    for it in sorted(items, key=lambda i: i["sold"], reverse=True)[:DETAIL_N]:
        try:
            it.update(parse_ali_detail(ali_call("aliexpress.ds.product.get", {
                "product_id": it["id"], "ship_to_country": "KR", "target_currency": "USD",
                "target_language": "EN"}), it["url"]))
        except MarketError:
            pass                    # 상세 1건 실패는 검색 결과만 씀
    return _summary("AliExpress", keyword, total, items)


# ---------------------------------------------------------------- eBay
_ebay_token = ("", 0.0)


def _ebay_auth():
    global _ebay_token
    tok, until = _ebay_token
    if tok and time.time() < until - 60:
        return tok
    cid, sec = os.environ.get("EBAY_CLIENT_ID", "").strip(), os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        raise MarketError(".env 에 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 없음")
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    try:
        d = _http(urllib.request.Request(EBAY_TOKEN_URL, data=body, method="POST", headers={
            "Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}))
    except MarketError as e:
        raise MarketError(f"토큰 발급 실패 ({e}) — .env 의 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 확인") from None
    tok = str(d.get("access_token") or "")
    if not tok:
        raise MarketError("토큰 발급 실패 (응답에 토큰 없음)")
    _ebay_token = (tok, time.time() + float(d.get("expires_in") or 7200))
    return tok


def ebay_get(path, params=None):
    url = f"{EBAY_API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    data = _http(urllib.request.Request(url, headers={"Authorization": f"Bearer {_ebay_auth()}",
                                                      "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}))
    if isinstance(data, dict) and data.get("errors"):
        e = data["errors"][0] or {}
        raise MarketError(f"API 오류 {e.get('errorId', '')} {e.get('message', '')}".strip())
    return data


def _seller(s):
    s = s or {}
    if not s.get("username"):
        return None
    return {"name": s["username"], "feedback_pct": _num(s.get("feedbackPercentage")),
            "feedback_score": _num(s.get("feedbackScore"), int)}


def parse_ebay_search(data):
    items = []
    for it in data.get("itemSummaries") or []:
        price = it.get("price") or {}
        if price.get("currency") != "USD" or not _num(price.get("value")):
            continue
        items.append({"id": str(it.get("itemId") or ""), "title": str(it.get("title") or "")[:100],
                      "url": str(it.get("itemWebUrl") or "").split("?")[0], "price_usd": _num(price["value"]),
                      "store": _seller(it.get("seller")),
                      "location": (it.get("itemLocation") or {}).get("country")})
    return _num(data.get("total"), int) or len(items), [i for i in items if i["id"]]


def parse_ebay_detail(data):
    out = {}
    avail = (data.get("estimatedAvailabilities") or [{}])[0] or {}
    if _num(avail.get("estimatedSoldQuantity"), int) is not None:
        out["sold"] = _num(avail.get("estimatedSoldQuantity"), int)
    aspects = {str(a.get("name")): str(a.get("value")) for a in data.get("localizedAspects") or []
               if a.get("name") and a.get("value")}
    if data.get("material") or aspects.get("Material"):
        out["material"] = data.get("material") or aspects["Material"]
    if aspects.get("Country of Origin") or aspects.get("Country/Region of Manufacture"):
        out["origin"] = aspects.get("Country of Origin") or aspects["Country/Region of Manufacture"]
    w = next((v for k, v in aspects.items() if re.search(r"weight", k, re.I)), None)
    if w:
        p = normalize_packaging({"unit_weight_kg": w, "evidence": [{"claim": f"eBay 상품 정보: 무게 {w}",
                                                                     "url": data.get("itemWebUrl")}]})
        if p:
            out["packaging"] = p
    return out


def ebay_snapshot(keyword):
    data = ebay_get("item_summary/search", {"q": keyword, "limit": str(SEARCH_N),
                                            "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE},"
                                                      "itemLocationCountry:US,priceCurrency:USD"})
    total, items = parse_ebay_search(data)
    for it in items[:DETAIL_N]:     # 판매 수는 상세에만 있어서 관련도 상위부터
        try:
            it.update(parse_ebay_detail(ebay_get("item/" + urllib.parse.quote(it["id"]))))
        except MarketError:
            pass
    return _summary("eBay", keyword, total, items)


# ---------------------------------------------------------------- 공통
def _summary(source, keyword, total, items):
    if not items:
        return {"source": source, "keyword": keyword, "total": total, "count": 0, "listings": []}
    prices = sorted(i["price_usd"] for i in items)
    detailed = [i for i in items if "store" in i and ("sold" in i or "packaging" in i)] or items
    top = sorted(detailed, key=lambda i: i.get("sold") or 0, reverse=True)[:DETAIL_N]
    return {"source": source, "keyword": keyword, "total": total, "count": len(items),
            "price_usd": {"min": prices[0], "median": round(statistics.median(prices), 2), "max": prices[-1]},
            "listings": top}


SOURCES = {"China": ("AliExpress", ali_snapshot), "USA": ("eBay", ebay_snapshot)}


def market_snapshot(request, log=print):
    """소싱 조건의 국가에 맞는 마켓 API 조회. 맞는 마켓이 없거나 실패하면 None (조사는 평소대로)."""
    countries = request.get("preferred_countries") or []
    keyword = request.get("product_en") or request.get("product")
    hit = next((SOURCES[c] for c in countries if c in SOURCES), None)
    if not hit or not keyword or request.get("market_api") is False:
        return None
    name, fn = hit
    try:
        snap = fn(keyword)
    except MarketError as e:
        log(f"      · {name} API 건너뜀: {e}")
        return None
    if snap["count"]:
        p = snap["price_usd"]
        log(f"      {name} API: 상품 {snap['total']:,}개 · 판매가 ${p['min']:g}~${p['max']:g} "
            f"(중간 ${p['median']:g}) · 상세 {len(snap['listings'])}건")
    else:
        log(f"      {name} API: '{keyword}' 검색 결과 없음")
    return snap


def _item_ids(urls):
    ids = set()
    for u in urls or []:
        # aliexpress.com/item/1005….html · ebay.com/itm/1234… 또는 /itm/상품명/1234…
        for m in re.finditer(r"/(?:item|itm)/(?:[^/?#]+/)?(\d{6,})", str(u)):
            ids.add(m.group(1))
    return ids


def enrich(result, snapshot):
    """검증 결과의 상품 페이지가 API로 조회한 상품이면, API 값(포장·무게)을 비어 있는 칸에 채움."""
    if not snapshot or not snapshot.get("listings"):
        return result
    ids = _item_ids(result.get("urls"))
    if not ids:
        return result
    for it in snapshot["listings"]:
        if _item_ids([it.get("url")]) & ids:
            if not result.get("packaging") and it.get("packaging"):
                result["packaging"] = it["packaging"]
            result["market_listing"] = {k: it.get(k) for k in ("source", "url", "price_usd", "sold", "rating",
                                                                "reviews", "store") if it.get(k) is not None}
            result["market_listing"]["source"] = snapshot["source"]
            break
    return result


def compact(snapshot):
    """AI 에게 넘길 요약 (토큰 절약: 포장 근거·상점 id 제외)."""
    if not snapshot:
        return None
    rows = []
    for it in snapshot.get("listings") or []:
        row = {k: it[k] for k in ("title", "url", "price_usd", "sold", "rating", "reviews", "material", "origin")
               if it.get(k) is not None}
        if it.get("store"):
            row["store"] = {k: v for k, v in it["store"].items() if k != "id" and v is not None}
        if it.get("packaging"):
            row["packaging"] = {k: it["packaging"][k] for k in ("per_unit_kg", "unit_size_cm")
                                if it["packaging"].get(k)}
        rows.append(row)
    return {"source": snapshot["source"], "keyword": snapshot["keyword"], "total": snapshot["total"],
            "price_usd": snapshot.get("price_usd"), "listings": rows}
