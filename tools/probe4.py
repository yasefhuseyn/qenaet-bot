import json, sys, re
import requests
sys.path.insert(0, ".")
import daily_updater as d
B = "https://b7x9kq.arazmarket.az"
def show(o, depth=0, maxd=3, key=""):
    pad = "  " * depth
    if isinstance(o, dict):
        print(f"{pad}{key} dict keys={list(o)[:14]}")
        if depth < maxd:
            for k, v in list(o.items())[:14]:
                if isinstance(v, (dict, list)): show(v, depth+1, maxd, k)
    elif isinstance(o, list):
        print(f"{pad}{key} list len={len(o)}" + (f" first={json.dumps(o[0], ensure_ascii=False)[:500]}" if o else ""))
        if o and isinstance(o[0], dict) and depth < maxd: show(o[0], depth+1, maxd, key+"[0]")
r = requests.get(B + "/api/home", headers=d.HEADERS, timeout=30)
show(r.json())
for p in ("api/categories", "api/campaigns", "api/campaign", "api/discount-magazine", "api/products/discounted", "api/product/discounted", "api/products?discount=1", "api/discounts", "api/home/products", "api/catalog/products", "api/market/products", "api/offers", "api/special-offers", "api/menu"):
    try:
        x = requests.get(f"{B}/{p}", headers=d.HEADERS, timeout=20)
        print(p, x.status_code, x.text[:160].replace("\n", " "))
    except Exception as e: print(p, "XƏTA", repr(e)[:60])
# Next.js page for products: look for fetch endpoints inside JS chunks
h = requests.get("https://www.arazmarket.az/az/products?discount=1", headers=d.HEADERS, timeout=30).text
chunks = re.findall(r'/_next/static/chunks/[\w./-]+\.js', h)
print("chunks", len(chunks))
found = set()
for c in chunks[:25]:
    try:
        t = requests.get("https://www.arazmarket.az" + c, headers=d.HEADERS, timeout=20).text
        for m in re.findall(r'["\'`](/?(?:api/)?[\w-]*(?:product|campaign|discount|catalog)[\w/-]*)["\'`]', t):
            found.add(m)
        for m in re.findall(r'https?://[\w.-]*arazmarket[\w./-]*', t): found.add(m)
    except Exception: pass
print(sorted(found)[:60])
