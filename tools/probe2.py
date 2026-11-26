import json, os, re, sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
sys.path.insert(0, ".")
import daily_updater as d

def card_samples(url, sels, n=2, size=1300):
    r = requests.get(url, headers=d.HEADERS, timeout=25)
    soup = BeautifulSoup(r.text, "html.parser")
    print(f"\n##### {url} HTTP {r.status_code} bytes={len(r.text)}")
    for sel in sels:
        els = soup.select(sel)
        print(f"  [{sel}] count={len(els)}")
        for el in els[:n]:
            print("    ", re.sub(r"\s+", " ", str(el))[:size])
    imgs = []
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-lazyloadsrc") or ""
        if src and not src.startswith("data:"):
            imgs.append(urljoin(url, src))
    print("  IMGS:", imgs[:8])
    return soup

card_samples("https://oba.az/products/", ['a.color-black'], 2, 1500)
card_samples("https://oba.az/xususi-teklifler/", ['[class*="product"]', 'a[href*="product"]'], 2, 900)
card_samples("https://www.arazmarket.az/", ['[class*="products-card_card"]'], 2, 1500)
card_samples("https://www.arazmarket.az/az/campaigns", ['[class*="campagins-item"]', '[class*="card"]'], 2, 800)
card_samples("https://bravosupermarket.az/special-offers/", ['.mobile_item', 'a[href*="leaflet"]'], 2, 800)
card_samples("https://bravosupermarket.az/special-offers/leaflet-5/", ['img'], 3, 300)
card_samples("https://rahatmarket.az/az/aksiyalar/rahat-market", ['.item', '.product_list'], 2, 1000)
card_samples("https://almarket.az/", ['.sub-item'], 2, 600)
card_samples("https://almarket.az/az/kataloqlar", ['img', 'a[href*="katalog"]'], 3, 400)
card_samples("https://bazarstore.az/endirimli-məhsullar", ['.product-item'], 2, 1800)

print("\n=== DDGS backends ===")
for backend in ("bing", "duckduckgo", "google", "auto"):
    for q in ("Coca-Cola 1L", "Bono tualet kağızı"):
        try:
            res = d.DDGS().images(q, max_results=4, backend=backend)
            print(backend, q, [(x.get("title", "")[:35], x.get("image", "")[:50]) for x in res][:3])
        except Exception as e:
            print(backend, q, "XƏTA", repr(e)[:120])
print("\n=== OpenFoodFacts ===")
for q in ("coca cola 1l", "tac kərə yağı", "bono tualet kağızı"):
    try:
        r = requests.get("https://world.openfoodfacts.org/cgi/search.pl", params={"search_terms": q, "search_simple": 1, "json": 1, "page_size": 5, "fields": "product_name,brands,image_front_url,countries_tags"}, headers=d.HEADERS, timeout=25)
        print(q, r.status_code, [(p.get("product_name"), p.get("brands"), bool(p.get("image_front_url"))) for p in r.json().get("products", [])][:4])
    except Exception as e:
        print(q, "XƏTA", repr(e)[:120])
print("\n=== Cashback search ===")
for backend in ("bing", "duckduckgo", "auto"):
    for q in ("Kapital Bank market keşbək kart", "PAŞA Bank market cashback kart", "Xalq Bank keşbək market"):
        try:
            res = d.DDGS().text(q, max_results=5, backend=backend)
            print(backend, q, [x.get("href") for x in res][:4])
        except Exception as e:
            print(backend, q, "XƏTA", repr(e)[:100])
