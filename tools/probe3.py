import json, re, sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
sys.path.insert(0, ".")
import daily_updater as d
def get(u):
    r = requests.get(u, headers=d.HEADERS, timeout=30); return r.text
# OBA card
h = get("https://oba.az/products/")
s = BeautifulSoup(h, "html.parser")
cards = s.select("div.product-cards a.color-black")
print("OBA cards:", len(cards))
for c in cards[:2]: print(re.sub(r"\s+", " ", str(c))[:2200])
print("OBA pagination/api hints:", re.findall(r"(?:data-page|load_more|loadmore|page=\d+|/api/[\w/?=&.-]+|ajax[\w/.-]*)", h)[:15])
print("OBA scripts w/ products:", [x.get("src") for x in s.find_all("script", src=True) if "oba" in (x.get("src") or "")][:8])
# Araz embedded data
h = get("https://www.arazmarket.az/")
print("\nARAZ html len", len(h))
for pat in (r'"price":[\d.]+', r'"name":"[^"]{4,60}"', r'b7x9kq\.arazmarket\.az/[\w/.-]*api[\w/.?=&-]*', r'/api/[\w/.?=&-]{3,80}', r'"discount[\w]*":'):
    m = re.findall(pat, h)
    print(pat, len(m), m[:6])
i = h.find('"price"')
print("ctx:", h[max(0, i-300):i+500] if i > 0 else "no price")
# Araz candidate APIs
for u in ("https://b7x9kq.arazmarket.az/api/products", "https://b7x9kq.arazmarket.az/api/v1/products", "https://b7x9kq.arazmarket.az/api/home", "https://www.arazmarket.az/api/products", "https://www.arazmarket.az/az/discounted-products", "https://www.arazmarket.az/az/products?discount=1"):
    try:
        r = requests.get(u, headers=d.HEADERS, timeout=20); print(u, r.status_code, r.headers.get("content-type"), r.text[:150].replace("\n", " "))
    except Exception as e: print(u, "XƏTA", repr(e)[:80])
# AL Market links
h = get("https://almarket.az/")
s = BeautifulSoup(h, "html.parser")
print("\nALMARKET links:", sorted({urljoin("https://almarket.az/", a["href"]) for a in s.find_all("a", href=True)})[:40])
h2 = get("https://almarket.az/az/kataloqlar")
print("ALMARKET kataloq text:", re.sub(r"\s+", " ", BeautifulSoup(h2, "html.parser").get_text(" "))[:400])
print("ALMARKET kataloq scripts:", re.findall(r"(?:https?:)?//[\w./-]*\.(?:pdf|jpg|png|webp|json)", h2)[:10])
# Bravo leaflet
h = get("https://bravosupermarket.az/special-offers/leaflet-5/")
print("\nBRAVO leaflet:", re.findall(r"[\w./:-]*(?:\.pdf|\.jpg|\.png|\.webp)", h)[:20])
print(re.sub(r"\s+", " ", BeautifulSoup(h, "html.parser").get_text(" "))[:300])
# Rahat base
h = get("https://rahatmarket.az/az/aksiyalar/rahat-market")
print("\nRAHAT base:", re.findall(r"<base[^>]*>", h), "imgs:", len(re.findall(r"aksiyalar/[\w_]+/[\w_-]+\.png", h)))
print(re.findall(r"assets/(?:cache/)?images/aksiyalar/[\w./_-]+", h)[:3])
# Neptun
r = requests.get("https://neptun.az/", headers=d.HEADERS, timeout=20); print("\nNEPTUN", r.status_code, len(r.text), r.headers.get("content-type"), r.text[:200])
# Bazarstore pagination
for u in ("https://bazarstore.az/endirimli-m%C9%99hsullar", "https://bazarstore.az/endirimli-m%C9%99hsullar?pagenumber=2", "https://bazarstore.az/endirimli-m%C9%99hsullar?pagenumber=3"):
    t = get(u); b = BeautifulSoup(t, "html.parser")
    print("BAZAR", u[-20:], len(b.select(".product-item")), [x.get_text(strip=True)[:25] for x in b.select(".product-title")][:3], [a.get("href") for a in b.select(".pager a")][:6])
