import sys, re
sys.path.insert(0, ".")
import daily_updater as d
from urllib.parse import quote
for q in ["Gülüm süd", "Nescafe", "Fairy"]:
    for url in (f"https://bazarstore.az/search?q={quote(q)}", f"https://bazarstore.az/search?q={quote(q)}&adv=false&cid=0&mid=0&vid=0&isc=true&orderby=0"):
        h = d.fetch_html(url, timeout=20) or ""
        items = d.parse_bazarstore_page(h)
        print("BAZAR", q, len(h), len(items), [(i["name"], i["price"]) for i in items[:3]], flush=True)
h = d.fetch_html("https://www.arazmarket.az/", timeout=20) or ""
m = re.search(r"https://([\w-]+\.arazmarket\.az)/storage", h)
host = m.group(1) if m else ""
print("ARAZ host", host)
for path in ("/api/search?q=nescafe", "/api/products?search=nescafe", "/api/search?query=nescafe", "/api/products/search?q=nescafe"):
    try:
        r = d.SESSION.get(f"https://{host}{path}", timeout=20)
        print("ARAZ", path, r.status_code, r.text[:300].replace("\n", " "), flush=True)
    except Exception as e:
        print("ARAZ", path, type(e).__name__)
