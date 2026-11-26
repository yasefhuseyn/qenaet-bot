"""Diaqnostika: real GitHub Actions mühitində saytların quruluşunu, Gemini modellərini və şəkil axtarışını yoxlayır."""
import json, os, re, sys
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
sys.path.insert(0, ".")
import daily_updater as d

PRICE = re.compile(r"\d+[.,]?\d*\s*(?:₼|AZN|azn|man\.?)")
print("=== GEMINI MODELLƏRİ ===")
try:
    from google import genai
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    names = []
    for m in c.models.list():
        acts = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
        if "generateContent" in acts:
            names.append(m.name)
    print("\n".join(sorted(names)))
except Exception as e:
    print("XƏTA:", repr(e)[:300])

print("\n=== SAYTLAR ===")
for market, url, dom in d.MARKET_WEBSITES:
    print(f"\n--- {market} {url}")
    try:
        r = requests.get(url, headers=d.HEADERS, timeout=20)
    except Exception as e:
        print("  XƏTA", repr(e)[:120]); continue
    soup = BeautifulSoup(r.text, "html.parser")
    ld = soup.find_all("script", type="application/ld+json")
    types = []
    for t in ld:
        try:
            j = json.loads(t.string or "")
            types.append(str(j.get("@type") if isinstance(j, dict) else [x.get("@type") for x in j if isinstance(x, dict)]))
        except Exception:
            types.append("?")
    nd = bool(soup.find("script", id="__NEXT_DATA__"))
    prices = PRICE.findall(soup.get_text(" "))
    cls = {}
    for el in soup.find_all(class_=True):
        for c_ in el.get("class"):
            if re.search(r"product|item|card|price|offer|discount", c_, re.I):
                cls[c_] = cls.get(c_, 0) + 1
    top = sorted(cls.items(), key=lambda x: -x[1])[:8]
    print(f"  HTTP {r.status_code} bytes={len(r.text)} title={soup.title.string.strip()[:60] if soup.title and soup.title.string else ''}")
    print(f"  ld+json={len(ld)} types={types[:4]} next_data={nd} imgs={len(soup.find_all('img'))} price_hits={len(prices)}")
    print(f"  top classes: {top}")
    print(f"  promo links: {d.discover_promo_links(BeautifulSoup(r.text,'html.parser'), url)[:6]}")
    # sample product-like card
    for sel in ('[class*="product-card"]', '[class*="product-item"]', '[class*="ProductCard"]', 'article', '[class*="product"]'):
        el = soup.select_one(sel)
        if el and PRICE.search(el.get_text(" ")):
            snippet = re.sub(r"\s+", " ", str(el))[:700]
            print("  sample[" + sel + "]: " + snippet)
            break
    # script src / api hints
    apis = set()
    for s in soup.find_all("script"):
        txt = (s.string or "")
        for m in re.findall(r"https?://[\w./-]*api[\w./?=&-]*", txt):
            apis.add(m[:100])
    print("  api hints:", list(apis)[:5])

print("\n=== ŞƏKİL AXTARIŞI (ddgs) ===")
try:
    for q in ["Bono tualet kağızı", "Tac kərə yağı 200q", "Coca-Cola 1L"]:
        res = d.DDGS().images(q, max_results=5)
        print(q, "->", [(x.get("title", "")[:40], x.get("width"), x.get("height")) for x in res][:4])
except Exception as e:
    print("XƏTA:", repr(e)[:300])

print("\n=== CASHBACK AXTARIŞI ===")
try:
    for bank, dom in d.SEARCH_BANKS:
        for q in (f"site:{dom} keşbək market", f"{bank} market cashback kart"):
            res = d.DDGS().text(q, max_results=5)
            print(bank, q, "->", [x.get("href") for x in res][:5])
except Exception as e:
    print("XƏTA:", repr(e)[:300])
