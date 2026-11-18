"""LLM 이 적은 링크·가격을 다음 단계(DB·원가 계산)에서 쓸 수 있는 형태로 정리."""
import re

# 업체를 '찾는 경로'일 뿐 특정 업체를 가리키지 않는 페이지 (검색 결과·카테고리·쇼룸·가이드)
LISTING_PATTERNS = [
    r"alibaba\.com/countrysearch/",
    r"alibaba\.com/showroom/",
    r"alibaba\.com/trade/search",
    r"//(?:electronics|sourcing|www)\.alibaba\.com/product/",   # 카테고리 안내 페이지 (상세는 /product-detail/)
    r"accio\.com/",
    r"made-in-china\.com/(?:suppliers|products-search|tag_search|multi-search)",
    r"//[a-z-]+\.made-in-china\.com/suppliers/",
    r"aliexpress\.[a-z.]+/w/",
    r"aliexpress\.[a-z.]+/(?:wholesale|popular)",
    r"s\.1688\.com/",
    r"s\.taobao\.com/",
    r"globalsources\.com/(?:searchList|manufacturers)",
    r"[?&](?:q|query|keyword|SearchText|searchText|k)=",
]
_LISTING = re.compile("|".join(LISTING_PATTERNS), re.I)


def is_listing_url(url) -> bool:
    return bool(url) and bool(_LISTING.search(str(url)))


def specific_urls(urls) -> list[str]:
    """업체·상품 고유 페이지만 남김 (순서 유지, 중복 제거)."""
    out = []
    for u in urls or []:
        u = str(u).strip()
        if u.startswith("http") and not is_listing_url(u) and u not in out:
            out.append(u)
    return out


# 그 품목을 실제로 파는 상품 상세 페이지 (스토어 첫 화면에는 해당 품목이 안 보일 수 있음)
PRODUCT_PATTERNS = [
    r"alibaba\.com/product-detail/",
    r"aliexpress\.[a-z.]+/item/",
    r"detail\.1688\.com/offer/",
    r"(?:item\.taobao|detail\.tmall)\.com/item",
    r"made-in-china\.com/product/",
    r"globalsources\.com/.+/p/",
    r"dhgate\.com/product/",
    r"amazon\.[a-z.]+/(?:.+/)?dp/",
]
_PRODUCT = re.compile("|".join(PRODUCT_PATTERNS), re.I)


def is_product_url(url) -> bool:
    return bool(url) and bool(_PRODUCT.search(str(url)))


def order_urls(urls) -> list[str]:
    """상품 상세 → 업체 고유 페이지(스토어·홈페이지) → 목록 페이지 순으로 정렬 (중복 제거)."""
    specific = specific_urls(urls)
    rest = [str(u) for u in urls or [] if u and str(u) not in specific]
    return ([u for u in specific if is_product_url(u)]
            + [u for u in specific if not is_product_url(u)]
            + list(dict.fromkeys(rest)))


def best_url(record: dict):
    """DB에 넣을 대표 링크: 상품 상세 페이지 → 공식 홈페이지 → 업체 고유 페이지. 목록 페이지뿐이면 None.

    누르면 '이 업체가 이 품목을 판다'는 게 바로 보이도록 상품 페이지를 가장 앞에 둠.
    """
    urls = record.get("urls") if isinstance(record.get("urls"), list) else [record.get("urls")]
    found = specific_urls(urls)
    products = [u for u in found if is_product_url(u)]
    if products:
        return products[0]
    site = record.get("website")
    if site and str(site).startswith("http") and not is_listing_url(site):
        return str(site)
    return found[0] if found else None


_NUM = r"(\d+(?:,\d{3})*(?:\.\d+)?)"
_USD = re.compile(rf"(?:USD|US\$|\$)\s*{_NUM}(?:\s*(?:-|~|–|to)\s*(?:USD|US\$|\$)?\s*{_NUM})?", re.I)


def parse_usd_price(text):
    """'USD 1.50-1.80', '$0.45~0.89', 'US$12' → 범위의 높은 값(USD).

    소량 구매는 보통 가격표의 비싼 쪽이 적용되므로, 원가 계산이 낙관적으로 나오지 않게 상한을 씀.
    달러 표기가 없으면(위안·엔 등) None — 원문은 price_note 에 그대로 남음.
    """
    if text is None:
        return None
    m = _USD.search(str(text))
    if not m:
        return None
    vals = [float(v.replace(",", "")) for v in m.groups() if v]
    price = max(vals)
    return price if 0 < price < 100000 else None
