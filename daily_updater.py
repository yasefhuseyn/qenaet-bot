"""
Qənaət — Super Bot v5.0
Hər səhər GitHub Actions üzərindən işləyir (Apify $5 pulsuz kredit + Gemini Free Tier):

 1. 10 marketin rəsmi saytını və saytdan avtomatik tapılan "endirim/aksiya/kampaniya" səhifələrini skan edir.
    Saytlardakı strukturlaşdırılmış məhsul məlumatını (JSON-LD) birbaşa oxuyur, qalan mətni Gemini ilə analiz edir.
 2. Saytlardan marketlərin Instagram, Facebook və Telegram ünvanlarını avtomatik tapır.
 3. APIFY_TOKEN varsa Instagram/Facebook paylaşımlarını, Telegram kanallarını isə pulsuz oxuyur.
 4. Endirim şəkillərini Gemini Vision ilə oxuyub məhsul + qiymət çıxarır.
 5. Eyni məhsulu marketlər arasında müqayisə edir, qiymət tarixçəsini hər gün yeniləyir,
    bitmiş endirimləri silir, hələ bitməyənləri saxlayır.
 6. Bank cashback səhifələrini (siyahıda olmayan bankları da axtarıb) oxuyur.
 7. Qiyməti düşən məhsulları və dəyişən cashback faizlərini `catalog/daily_changes` sənədinə yazır.

Lazım olan gizli açarlar: FIREBASE_SERVICE_ACCOUNT, GEMINI_API_KEY, (istəyə görə) APIFY_TOKEN.
İstəyə görə mühit dəyişənləri (hamısının standart qiyməti var):
  DRY_RUN=1                Firestore-a yazmır, nəticəni out/ qovluğuna JSON kimi yazır
  IG_POSTS_PER_PAGE=3      Instagram hesabından neçə paylaşım (Apify xərci buna bağlıdır)
  FB_POSTS_PER_PAGE=3      Facebook səhifəsindən neçə paylaşım
  TG_POSTS_PER_CHANNEL=12  Telegram kanalından neçə paylaşım
  VISION_MAX_IMAGES=15     Vision AI ilə oxunacaq maksimum şəkil
  EXTRA_PAGES_PER_MARKET=3 Hər saytda əlavə baxılacaq endirim səhifəsi
  IMAGE_SEARCH_MAX=40      İnternetdə məhsul şəkli axtarışı limiti
  TELEGRAM_EXTRA="Bravo:kanal1,OBA:kanal2"   əlavə Telegram kanalları
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:  # DRY_RUN üçün Firebase lazım deyil
    firebase_admin = credentials = firestore = None

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = types = None

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

log = logging.getLogger("qenaet")

BAKU_TZ = timezone(timedelta(hours=4))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36",
    "Accept-Language": "az,en;q=0.8,ru;q=0.6",
}


def env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


IG_POSTS = env_int("IG_POSTS_PER_PAGE", 3)
FB_POSTS = env_int("FB_POSTS_PER_PAGE", 3)
TG_POSTS = env_int("TG_POSTS_PER_CHANNEL", 12)
VISION_MAX = env_int("VISION_MAX_IMAGES", 15)
EXTRA_PAGES = env_int("EXTRA_PAGES_PER_MARKET", 3)
IMAGE_SEARCH_MAX = env_int("IMAGE_SEARCH_MAX", 40)
MAX_PRODUCTS = 120
MAX_AGE_DAYS = 10          # bundan köhnə sosial şəbəkə paylaşımları atılır
MIN_PRICE, MAX_PRICE = 0.05, 300.0
GEMINI_BATCH_CHARS = 22000
GEMINI_MAX_BATCHES = 4
GEMINI_MIN_INTERVAL = 4.0  # saniyə (pulsuz limitə uyğun)

WATERMARK_DOMAINS = (
    "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    "dreamstime.com", "123rf.com", "depositphotos.com", "vecteezy.com", "freepik.com",
)

MARKET_WEBSITES = [
    ("OBA", "https://oba.az/products/", "oba.az"),
    ("Araz", "https://www.arazmarket.az/", "arazmarket.az"),
    ("Bravo", "https://bravosupermarket.az/special-offers/", "bravosupermarket.az"),
    ("Rahat", "https://rahatmarket.az/az/aksiyalar/rahat-market", "rahatmarket.az"),
    ("Bazarstore", "https://bazarstore.az/", "bazarstore.az"),
    ("AL Market", "https://almarket.az/", "almarket.az"),
    ("Neptun", "https://neptun.az/", "neptun.az"),
    ("Grandmart", "https://www.grandmartsupermarket.com/", "grandmartsupermarket.com"),
    ("Bolmart", "https://www.bolmart.az/", "bolmart.az"),
    ("Spar", "https://spar-international.com/country/azerbaijan-2/", "spar-international.com"),
]

# Rəsmi saytlardan avtomatik tapılmazsa ehtiyat ünvanlar
DEFAULT_SOCIAL_URLS = {
    "Bravo":      {"ig": "https://www.instagram.com/bravosupermarketaz/", "fb": "https://www.facebook.com/BravoSupermarketAz/"},
    "Araz":       {"ig": "https://www.instagram.com/arazsupermarket/", "fb": "https://www.facebook.com/arazsupermarket/"},
    "Bazarstore": {"ig": "https://www.instagram.com/bazarstore_az/", "fb": "https://www.facebook.com/Bazarstore/"},
    "OBA":        {"ig": "https://www.instagram.com/obamarketler/", "fb": "https://www.facebook.com/obamarketler/"},
    "AL Market":  {"ig": "https://www.instagram.com/almarket.az/", "fb": "https://www.facebook.com/almarket.az/"},
    "Rahat":      {"ig": "https://www.instagram.com/rahatmarket/", "fb": "https://www.facebook.com/rahatmarket/"},
    "Neptun":     {"ig": "https://www.instagram.com/neptunsupermarket/", "fb": "https://www.facebook.com/neptunsupermarket/"},
    "Bolmart":    {"ig": "https://www.instagram.com/bolmart.az/", "fb": "https://www.facebook.com/bolmart.az/"},
    "Grandmart":  {"ig": "https://www.instagram.com/grandmart.az/", "fb": "https://www.facebook.com/grandmart.az/"},
}

TELEGRAM_CHANNELS = {
    "Bravo": "bravosupermarketaz",
    "Araz": "arazsupermarket",
    "OBA": "obamarketaz",
    "AL Market": "almarketaz",
    "Bazarstore": "bazarstore_az",
}

CASHBACK_SOURCES = [
    ("Birbank", "https://birbank.az/taksit-karti-ile-bonus-qazan", "birbank.az"),
    ("ABB", "https://abb-bank.az/en/ferdi/kesbek/faydali-kesbek", "abb-bank.az"),
    ("Leobank", "https://leobank.az/az/cashback", "leobank.az"),
    ("Yelo Bank", "https://www.yelo.az/az/individuals/cashback/", "yelo.az"),
    ("Rabitəbank", "https://www.rabitabank.com/ferdi/kesbek-1", "rabitabank.com"),
    ("Unibank", "https://unibank.az/az/cards/cashback", "unibank.az"),
    ("FED.az", "https://fed.az/az/bank/hansi-bankin-karti-ile-markete-getmek-daha-serfelidir-banklar-kesbekler-siyahi-avqust-2026-262649", "fed.az"),
]

# Siyahıda hazır ünvanı olmayan banklar: səhifəni özü axtarıb tapır
SEARCH_BANKS = [
    ("Kapital Bank", "kapitalbank.az"),
    ("PAŞA Bank", "pashabank.az"),
    ("Xalq Bank", "xalqbank.az"),
]

BANK_GRADIENTS = {
    "Birbank": "from-rose-600 via-red-600 to-rose-800",
    "Kapital Bank": "from-rose-600 via-red-600 to-rose-800",
    "ABB": "from-blue-700 via-blue-800 to-indigo-950",
    "Leobank": "from-amber-400 via-yellow-500 to-amber-600",
    "Yelo Bank": "from-amber-400 via-yellow-500 to-amber-600",
    "Rabitəbank": "from-emerald-600 via-green-700 to-teal-900",
    "Unibank": "from-sky-600 via-blue-700 to-indigo-800",
    "PAŞA Bank": "from-slate-700 via-slate-800 to-slate-950",
    "Xalq Bank": "from-cyan-600 via-sky-700 to-blue-900",
}
DEFAULT_GRADIENT = "from-slate-700 via-slate-800 to-slate-950"

CANONICAL_MARKETS = ["Bravo", "Araz", "Bazarstore", "Rahat", "OBA", "AL Market", "Neptun", "Grandmart", "Spar", "Bolmart"]
MARKET_ALIASES = {
    "bravo": "Bravo", "araz": "Araz", "bazar": "Bazarstore", "rahat": "Rahat", "oba": "OBA",
    "almarket": "AL Market", "al market": "AL Market", "al-market": "AL Market", "neptun": "Neptun",
    "grandmart": "Grandmart", "grand mart": "Grandmart", "spar": "Spar", "bolmart": "Bolmart", "bol mart": "Bolmart",
}
ALLOWED_CATEGORIES = ("Süd məhsulları", "Ərzaq", "İçkilər", "Təmizlik", "Şirniyyat")
PROMO_KEYWORDS = ("endirim", "aksiya", "kampaniya", "special", "promo", "offer", "discount", "kataloq",
                  "catalog", "teklif", "təklif", "sale", "kampaniy")
PRICE_RE = re.compile(r"\d+(?:[.,]\d{1,2})?\s*(?:₼|azn|man\b|manat)", re.I)

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash-lite"]

STATS = {"web_pages": 0, "structured": 0, "ig_posts": 0, "fb_posts": 0, "tg_posts": 0,
         "vision_images": 0, "gemini_calls": 0, "gemini_errors": 0, "image_searches": 0}


# --------------------------------------------------------------------------- ümumi köməkçilər
def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=2, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


def fetch_html(url: str, timeout: int = 16) -> str:
    try:
        r = SESSION.get(url, timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
        log.warning("  [Veb] %s → HTTP %s", url, r.status_code)
    except Exception as exc:
        log.warning("  [Veb xəta] %s: %s", url, exc)
    return ""


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9əöüğçşı]+", "", (name or "").lower())[:40]


def clean_text(value, limit: int = 95) -> str:
    """Saytda innerHTML işlədildiyi üçün təhlükəli simvolları silir."""
    return re.sub(r"[<>\"`\\\x00-\x1f]", "", str(value or "")).strip()[:limit]


def safe_float(value, default=None):
    try:
        return float(str(value).replace(",", ".").replace("%", "").replace("₼", "").strip())
    except Exception:
        return default


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


def normalize_market(name: str) -> str:
    low = (name or "").lower()
    for alias in sorted(MARKET_ALIASES, key=len, reverse=True):
        if re.search(r"(?<![a-zəöüğçşı])" + re.escape(alias), low):
            return MARKET_ALIASES[alias]
    return clean_text(name, 30)


def is_recent(iso_value, days: int = MAX_AGE_DAYS) -> bool:
    if not iso_value:
        return True
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(days=days)
    except Exception:
        return True


def needs_vision(text: str) -> bool:
    """Mətndə qiymət yoxdursa endirim şəkildədir — şəkli oxumaq lazımdır."""
    return not PRICE_RE.search(text or "") or len(text or "") < 80


def extract_json_array(raw_text: str) -> list:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return []
    try:
        data = json.loads(raw_text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
    except Exception:
        pass
    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, list) else []
    except Exception:
        return []


# --------------------------------------------------------------------------- Gemini
_last_gemini_call = 0.0


def gemini_text(client, contents) -> str:
    """Gemini-yə sorğu göndərir; model ardıcıllığı, gözləmə və təkrar cəhd daxildir. Mətn qaytarır."""
    global _last_gemini_call
    config = None
    if types is not None:
        try:
            config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
        except Exception:
            config = None
    last_err = None
    for model_name in GEMINI_MODELS:
        for attempt in range(3):
            wait = GEMINI_MIN_INTERVAL - (time.time() - _last_gemini_call)
            if wait > 0:
                time.sleep(wait)
            try:
                _last_gemini_call = time.time()
                STATS["gemini_calls"] += 1
                kwargs = {"model": model_name, "contents": contents}
                if config is not None:
                    kwargs["config"] = config
                resp = client.models.generate_content(**kwargs)
                return getattr(resp, "text", None) or ""
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                if "404" in msg or "NOT_FOUND" in msg:
                    break  # növbəti modelə keç
                if any(k in msg for k in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")):
                    w = 8 * (attempt + 1)
                    log.warning("  [Gemini %s] müvəqqəti yüklənmə (%d/3), %ds gözlənilir...", model_name, attempt + 1, w)
                    time.sleep(w)
                    continue
                STATS["gemini_errors"] += 1
                raise
    STATS["gemini_errors"] += 1
    raise last_err or RuntimeError("Gemini cavab vermədi")


# --------------------------------------------------------------------------- şəkillər
def verify_image_url(url: str, min_dim: int = 220) -> bool:
    if not url or not url.startswith("https://"):
        return False
    host = urlparse(url).netloc.lower()
    if any(wm in host for wm in WATERMARK_DOMAINS):
        return False
    r = None
    try:
        r = SESSION.get(url, timeout=8, stream=True)
        if r.status_code != 200 or "image" not in (r.headers.get("Content-Type") or "").lower():
            return False
        chunk = r.raw.read(65536)
        im = Image.open(io.BytesIO(chunk))
        w, h = im.size
        if w < min_dim or h < min_dim:
            return False
        return 0.45 <= w / float(h) <= 2.1
    except Exception:
        return False
    finally:
        if r is not None:
            r.close()


def find_high_quality_product_image(product_name: str, image_cache: dict) -> str:
    key = normalize_key(product_name)
    if image_cache.get(key):
        return image_cache[key]
    if DDGS is None:
        return ""
    STATS["image_searches"] += 1
    for q in (f'"{product_name}"', f"{product_name} product"):
        try:
            time.sleep(0.6)
            results = DDGS().images(q, max_results=6)
            for item in results or []:
                img_url = item.get("image") or ""
                w = int(item.get("width") or 0)
                h = int(item.get("height") or 0)
                if w and h and (w < 250 or h < 250):
                    continue
                if verify_image_url(img_url, min_dim=220):
                    image_cache[key] = img_url
                    return img_url
        except Exception as exc:
            log.warning("  [Şəkil axtarış xətası] %s: %s", product_name, exc)
            break
    return ""


def collect_brand_logos(existing_logos: dict) -> dict:
    logos = dict(existing_logos or {})
    brands = [(m, dom) for m, _, dom in MARKET_WEBSITES]
    brands += [(b, dom) for b, _, dom in CASHBACK_SOURCES if b != "FED.az"]
    brands += list(SEARCH_BANKS)
    todo = [(n, d) for n, d in brands if not (logos.get(n) or "").startswith("https://")]

    def find(item):
        name, domain = item
        found = ""
        html = fetch_html(f"https://{domain}", timeout=10)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for sel, attr in (('link[rel="apple-touch-icon"]', "href"), ('meta[property="og:image"]', "content")):
                tag = soup.select_one(sel)
                if tag and tag.get(attr):
                    cand = urljoin(f"https://{domain}", tag.get(attr))
                    if cand.startswith("https://"):
                        found = cand
                        break
        return name, found or f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, url in pool.map(find, todo):
            logos[name] = url
    return logos


# --------------------------------------------------------------------------- veb saytlar
def extract_structured_products(soup, market: str, page_url: str, promo_page: bool) -> list[dict]:
    """JSON-LD (schema.org Product/Offer) məlumatını birbaşa oxuyur — Gemini lazım deyil."""
    items = []

    def first_image(img):
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url") or ""
        return urljoin(page_url, str(img)) if img else ""

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        t = node.get("@type")
        type_list = t if isinstance(t, list) else [t]
        if "Product" in type_list and node.get("name"):
            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else ([offers] if offers else [])
            for off in offer_list:
                if not isinstance(off, dict):
                    continue
                cur = str(off.get("priceCurrency") or "AZN").upper()
                price = safe_float(off.get("price") if off.get("price") is not None else off.get("lowPrice"))
                if not price or cur not in ("AZN", "AZ", "MANAT"):
                    continue
                old = None
                specs = off.get("priceSpecification")
                for spec in (specs if isinstance(specs, list) else [specs] if specs else []):
                    if isinstance(spec, dict) and re.search(r"list|strike", str(spec.get("priceType") or ""), re.I):
                        old = safe_float(spec.get("price"))
                if not old and not promo_page:
                    break  # endirim əlaməti yoxdur — adi kataloq qiymətidir
                items.append({
                    "name": node.get("name"), "price": price, "oldPrice": old, "market": market,
                    "source": "Veb sayt", "imageUrl": first_image(node.get("image")),
                    "endDate": str(off.get("priceValidUntil") or "")[:10],
                    "category": node.get("category") if isinstance(node.get("category"), str) else "",
                })
                break
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or tag.get_text() or ""))
        except Exception:
            continue
    return items[:60]


def same_site(a: str, b: str) -> bool:
    strip = lambda u: urlparse(u).netloc.lower().removeprefix("www.")
    return strip(a) == strip(b)


def discover_promo_links(soup, base_url: str) -> list[str]:
    found, seen = [], {base_url.split("#")[0]}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(base_url, href).split("#")[0]
        if full in seen or not same_site(full, base_url):
            continue
        low = (full + " " + a.get_text(" ", strip=True)).lower()
        if any(k in low for k in PROMO_KEYWORDS) and not re.search(r"\.(pdf|jpg|jpeg|png|zip)$", full.lower()):
            seen.add(full)
            found.append(full)
    return found


def discover_socials(soup, socials: dict, tg: dict):
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        path = [p for p in urlparse(href).path.split("/") if p]
        if "instagram.com/" in low and path and not any(x in low for x in ("/p/", "/reel", "/explore", "/share")):
            socials.setdefault("ig", href.split("?")[0])
        elif "facebook.com/" in low and path and not any(x in low for x in ("sharer", "/posts/", "/share", "/plugins", "/dialog", "/tr?")):
            socials.setdefault("fb", href.split("?")[0])
        elif "t.me/" in low and path and "share" not in low:
            slug = path[1] if path[0] == "s" and len(path) > 1 else path[0]
            if slug and not slug.startswith("+") and slug != "joinchat":
                tg.setdefault("slug", slug)


def page_to_block(soup, market: str, page_url: str) -> dict | None:
    for tag in soup(["script", "style", "noscript", "svg", "footer", "nav"]):
        tag.decompose()
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src or src.startswith("data:"):
            continue
        full = urljoin(page_url, src)
        low = full.lower()
        if any(skip in low for skip in ("logo", "icon", "favicon", "flag", "sprite", "avatar", "banner")):
            continue
        if any(ext in low for ext in (".png", ".jpg", ".jpeg", ".webp")):
            images.append({"alt": (img.get("alt") or "").strip()[:80], "url": full})
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if len(text) < 120:
        return None
    return {"market": market, "source": "Veb sayt", "url": page_url, "text": text[:4000], "images": images[:25]}


def scrape_one_market(market: str, url: str) -> dict:
    out = {"market": market, "blocks": [], "structured": [], "socials": {}, "telegram": ""}
    html = fetch_html(url)
    if not html:
        return out
    soup0 = BeautifulSoup(html, "html.parser")
    pages = [(url, soup0)]
    for extra in discover_promo_links(soup0, url)[:EXTRA_PAGES]:
        h = fetch_html(extra, timeout=14)
        if h:
            pages.append((extra, BeautifulSoup(h, "html.parser")))

    tg = {}
    for page_url, soup in pages:
        promo_page = any(k in page_url.lower() for k in PROMO_KEYWORDS)
        discover_socials(soup, out["socials"], tg)
        out["structured"] += extract_structured_products(soup, market, page_url, promo_page)
        block = page_to_block(soup, market, page_url)  # soup-u dəyişir, sonda çağırılır
        if block:
            out["blocks"].append(block)
        STATS["web_pages"] += 1
    out["telegram"] = tg.get("slug", "")
    log.info("  [Veb] %s: %d səhifə, %d strukturlu məhsul, ig=%s fb=%s tg=%s", market, len(pages), len(out["structured"]),
             bool(out["socials"].get("ig")), bool(out["socials"].get("fb")), out["telegram"] or "-")
    return out


def scrape_market_websites() -> tuple[list[dict], list[dict], dict, dict]:
    socials = {k: dict(v) for k, v in DEFAULT_SOCIAL_URLS.items()}
    telegram = dict(TELEGRAM_CHANNELS)
    blocks, structured = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda m: scrape_one_market(m[0], m[1]), MARKET_WEBSITES))
    for res in results:
        blocks += res["blocks"]
        structured += res["structured"]
        if res["socials"]:
            socials.setdefault(res["market"], {}).update(res["socials"])  # saytdan tapılan rəsmi ünvan üstünlük qazanır
        if res["telegram"]:
            telegram[res["market"]] = res["telegram"]
    for pair in (os.environ.get("TELEGRAM_EXTRA") or "").split(","):
        if ":" in pair:
            m, slug = pair.split(":", 1)
            telegram[m.strip()] = slug.strip().lstrip("@")
    STATS["structured"] = len(structured)
    return blocks, structured, socials, telegram


# --------------------------------------------------------------------------- Apify (Instagram, Facebook)
def apify_run(actor: str, token: str, payload: dict, label: str) -> list:
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    try:
        r = requests.post(endpoint, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=240)
        if r.status_code not in (200, 201):
            log.error("  [Apify %s xəta] HTTP %s: %s", label, r.status_code, r.text[:200])
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.error("  [Apify %s xəta] %s", label, type(exc).__name__)
        return []


def scrape_instagram_via_apify(token: str, socials: dict) -> tuple[list[dict], list[dict]]:
    ig_urls, handle_to_market = [], {}
    for market, links in socials.items():
        ig = links.get("ig")
        if ig and "instagram.com/" in ig:
            ig_urls.append(ig)
            handle = urlparse(ig).path.strip("/").split("/")[0].lower()
            if handle:
                handle_to_market[handle] = market
    if not ig_urls:
        return [], []
    log.info("  [Apify Instagram] %d hesab (hərəsindən son %d paylaşım)...", len(ig_urls), IG_POSTS)
    posts = apify_run("apify~instagram-scraper", token,
                      {"directUrls": ig_urls[:12], "resultsType": "posts", "resultsLimit": IG_POSTS}, "Instagram")
    blocks, vision = [], []
    for post in posts:
        if not isinstance(post, dict) or not is_recent(post.get("timestamp")):
            continue
        STATS["ig_posts"] += 1
        owner = (post.get("ownerUsername") or "").lower()
        market = handle_to_market.get(owner, owner or "Instagram")
        caption = (post.get("caption") or "").strip()
        img = post.get("displayUrl") or ""
        if caption:
            blocks.append({"market": market, "source": "Instagram", "text": caption[:700], "imageUrl": img})
        if img and needs_vision(caption):
            vision.append({"market": market, "source": "Instagram", "imageUrl": img})
    return blocks, vision


def scrape_facebook_via_apify(token: str, socials: dict) -> tuple[list[dict], list[dict]]:
    fb_urls = [l["fb"] for l in socials.values() if l.get("fb") and "facebook.com/" in l["fb"]]
    if not fb_urls:
        return [], []
    log.info("  [Apify Facebook] %d səhifə (hərəsindən son %d paylaşım)...", len(fb_urls), FB_POSTS)
    posts = apify_run("automly~facebook-page-posts-scraper", token,
                      {"pages": fb_urls[:12], "maxPosts": FB_POSTS}, "Facebook")
    blocks, vision = [], []
    for post in posts:
        if not isinstance(post, dict):
            continue
        STATS["fb_posts"] += 1
        page = post.get("pageName") or (post.get("user") or {}).get("name") or "Facebook"
        text = (post.get("text") or post.get("postText") or "").strip()
        media = post.get("media") or []
        img = ""
        if isinstance(media, list) and media and isinstance(media[0], dict):
            first = media[0]
            img = first.get("thumbnail") or (first.get("photo_image") or {}).get("uri") or first.get("url") or ""
        if text:
            blocks.append({"market": page, "source": "Facebook", "text": text[:700], "imageUrl": img})
        if img and needs_vision(text):
            vision.append({"market": page, "source": "Facebook", "imageUrl": img})
    return blocks, vision


# --------------------------------------------------------------------------- Telegram
def scrape_one_telegram(market: str, slug: str) -> tuple[list[dict], list[dict]]:
    html = fetch_html(f"https://t.me/s/{slug}", timeout=15)
    if not html:
        return [], []
    soup = BeautifulSoup(html, "html.parser")
    posts, vision = [], []
    for wrap in soup.select(".tgme_widget_message_wrap")[-TG_POSTS:]:
        time_el = wrap.select_one("time")
        if not is_recent(time_el.get("datetime") if time_el else None):
            continue
        txt_el = wrap.select_one(".tgme_widget_message_text")
        text = txt_el.get_text(" ", strip=True) if txt_el else ""
        img = ""
        photo = wrap.select_one(".tgme_widget_message_photo_wrap")
        if photo and photo.get("style"):
            m = re.search(r"url\(['\"]?(https?://[^'\")]+)", photo["style"])
            if m:
                img = m.group(1)
        STATS["tg_posts"] += 1
        if len(text) > 15:
            posts.append({"text": text[:500], "imageUrl": img})
        if img and needs_vision(text):
            vision.append({"market": market, "source": "Telegram kanalı", "imageUrl": img})
    blocks = [{"market": market, "source": "Telegram kanalı", "posts": posts}] if posts else []
    return blocks, vision


def scrape_telegram_channels(channels: dict) -> tuple[list[dict], list[dict]]:
    blocks, vision = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for b, v in pool.map(lambda kv: scrape_one_telegram(*kv), channels.items()):
            blocks += b
            vision += v
    return blocks, vision


# --------------------------------------------------------------------------- Vision AI
def extract_from_promo_images_vision(client, candidates: list[dict]) -> list[dict]:
    extracted, seen = [], set()
    for cand in candidates:
        url = cand.get("imageUrl")
        if not url or url in seen:
            continue
        seen.add(url)
        if len(seen) > VISION_MAX:
            break
        try:
            r = SESSION.get(url, timeout=12)
            if r.status_code != 200 or len(r.content) > 4_500_000:
                continue
            mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                mime = "image/jpeg"
            part = types.Part.from_bytes(data=r.content, mime_type=mime)
            prompt = (
                f"Bu {cand['market']} marketinin sosial şəbəkə endirim paylaşımıdır. "
                "Şəkildə konkret qiyməti (AZN / ₼) görünən bütün məhsulları YALNIZ JSON massivi kimi qaytar: "
                '[{"name": "...", "category": "Ərzaq", "oldPrice": 3.5, "price": 2.49, "unit": "1 ədəd", "endDate": "2026-12-31"}]. '
                "endDate yalnız şəkildə yazılıbsa (YYYY-MM-DD). Qiymət yoxdursa [] qaytar. Heç nə uydurma."
            )
            STATS["vision_images"] += 1
            for item in extract_json_array(gemini_text(client, [prompt, part])):
                if isinstance(item, dict) and item.get("name") and item.get("price"):
                    item["market"] = cand["market"]
                    item["source"] = cand.get("source", "Sosial şəbəkə")
                    extracted.append(item)
        except Exception as exc:
            log.warning("  [Vision xətası] %s: %s", cand.get("market"), exc)
    log.info("  [Vision AI] %d şəkildən %d məhsul oxundu", STATS["vision_images"], len(extracted))
    return extracted


# --------------------------------------------------------------------------- məhsulların təhlili
def pick_svg_type(category: str, name: str) -> str:
    low = (name + " " + category).lower()
    if any(k in low for k in ("kərə yağ", "kere yag", "kərə", "kere")):
        return "butter"
    if any(k in low for k in ("qatıq", "qatiq", "yoqurt", "kefir", "ayran")):
        return "yogurt"
    if any(k in low for k in ("süd", "sud", "pendir")):
        return "milk"
    if any(k in low for k in ("yağ", "yag")):
        return "oil"
    if any(k in low for k in ("çay", "cay", "qəhvə", "kofe")):
        return "tea"
    if any(k in low for k in ("sabun", "fairy", "bulaşıq")):
        return "soap"
    if any(k in low for k in ("yuyucu", "ariel", "persil", "təmiz")):
        return "detergent"
    if any(k in low for k in ("kola", "cola", "su ", "şirə", "içki", "fanta")):
        return "drink"
    return "milk"


def chunk_blocks(blocks: list[dict]) -> list[list[dict]]:
    batches, cur, size = [], [], 0
    for b in blocks:
        n = len(json.dumps(b, ensure_ascii=False))
        if cur and size + n > GEMINI_BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
    if cur:
        batches.append(cur)
    return batches[:GEMINI_MAX_BATCHES]


def gemini_extract_products(client, blocks: list[dict]) -> list[dict]:
    items = []
    for i, batch in enumerate(chunk_blocks(blocks), 1):
        prompt = (
            "Sən Azərbaycan supermarket endirimlərini analiz edən peşəkar AI sistemsən.\n"
            "Aşağıdakı veb sayt, Instagram, Facebook və Telegram məlumatlarından REAL qiyməti (AZN) olan "
            "endirimli məhsulları çıxar.\n"
            "Qaydalar:\n"
            "1. Yalnız real məhsul adı və rəqəmsal qiyməti olanları götür; heç nə uydurma.\n"
            "2. oldPrice yalnız mətndə köhnə qiymət yazılıbsa doldurulur, yoxdursa boş burax.\n"
            "3. endDate yalnız mətndə bitmə tarixi yazılıbsa (YYYY-MM-DD), yoxdursa boş burax.\n"
            "4. imageUrl yalnız veb sayt blokundakı `images` siyahısından tək məhsulun şəkli ola bilər; "
            "toplu kataloq/post şəkillərini yazma.\n"
            "5. market: Bravo, Araz, Bazarstore, Rahat, OBA, AL Market, Neptun, Grandmart, Spar, Bolmart.\n"
            "6. YALNIZ JSON massivi qaytar (maksimum 60 məhsul). Sahələr: name, category "
            '("Süd məhsulları"|"Ərzaq"|"İçkilər"|"Təmizlik"|"Şirniyyat"), market, '
            'source ("Instagram"|"Facebook"|"Veb sayt"|"Telegram kanalı"), oldPrice, price, unit, imageUrl, endDate.\n\n'
            + json.dumps(batch, ensure_ascii=False)
        )
        try:
            got = [x for x in extract_json_array(gemini_text(client, prompt)) if isinstance(x, dict)]
            log.info("  [Gemini] partiya %d: %d məhsul", i, len(got))
            items += got
        except Exception as exc:
            log.error("  [Gemini xəta] partiya %d: %s", i, exc)
    return items


def clean_item(it: dict, today: str) -> dict | None:
    """Xam məhsulu yoxlayır və təmizləyir; yararsızdırsa None qaytarır."""
    if not isinstance(it, dict):
        return None
    name = clean_text(it.get("name"), 95)
    price = safe_float(it.get("price"))
    if not name or price is None or not (MIN_PRICE <= price <= MAX_PRICE):
        return None
    price = round(price, 2)
    old = safe_float(it.get("oldPrice"), 0) or 0
    old = round(old, 2) if price < old <= price * 5 else 0   # uydurma/yanlış köhnə qiymət atılır
    end_date = ""
    raw_end = str(it.get("endDate") or "").strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_end):
        if raw_end < today:
            return None  # bitmiş endirim
        end_date = raw_end
    cat = clean_text(it.get("category"), 30)
    if cat not in ALLOWED_CATEGORIES:
        cat = "Ərzaq"
    key = normalize_key(name)
    if not key:
        return None
    img = clean_text(it.get("imageUrl"), 600)
    return {
        "key": key, "name": name, "category": cat, "market": normalize_market(it.get("market") or ""),
        "source": clean_text(it.get("source") or "Veb sayt", 30), "price": price, "oldPrice": old,
        "unit": clean_text(it.get("unit") or "1 ədəd", 25), "imageUrl": img if img.startswith("https://") else "",
        "endDate": end_date,
    }


def build_products(raw_items: list[dict], prev_items: list[dict], image_cache: dict, today: str) -> tuple[list[dict], list[dict]]:
    """Təmizləyir, təkrarları birləşdirir, marketlər arası müqayisə qurur, tarixçə və şəkilləri yeniləyir."""
    # 1. Təmizlə və (məhsul, market) üzrə ən ucuz təklifi saxla
    entries: dict[str, dict] = {}
    for it in raw_items:
        e = clean_item(it, today)
        if not e:
            continue
        hk = f"{e['key']}|{e['market']}"
        if hk not in entries or e["price"] < entries[hk]["price"]:
            entries[hk] = e

    # 2. Əvvəlki məlumatlar
    prev_index = {}
    for p in prev_items:
        k = normalize_key(p.get("name", ""))
        hk = p.get("histKey") or f"{k}|{normalize_market(p.get('market', ''))}"
        prev_index[hk] = p

    # 3. Bu gün tapılmayan, amma bitmə tarixi çatmamış endirimləri saxla
    carried = 0
    for p in prev_items:
        end = str(p.get("endDate") or "")
        k = normalize_key(p.get("name", ""))
        market = normalize_market(p.get("market", ""))
        hk = f"{k}|{market}"
        if end and end >= today and hk not in entries and k:
            entries[hk] = {
                "key": k, "name": clean_text(p.get("name")), "category": p.get("category", "Ərzaq"), "market": market,
                "source": clean_text(p.get("source") or "Veb sayt", 30), "price": safe_float(p.get("price"), 0),
                "oldPrice": safe_float(p.get("oldPrice"), 0) or 0, "unit": clean_text(p.get("unit") or "1 ədəd", 25),
                "imageUrl": p.get("imageUrl") or "", "endDate": end, "carried": True,
            }
            carried += 1
    if carried:
        log.info("  Bitmə tarixi çatmamış %d əvvəlki endirim saxlanıldı", carried)

    # 4. Marketlər arası qruplaşdırma
    groups: dict[str, list[dict]] = {}
    for e in entries.values():
        groups.setdefault(e["key"], []).append(e)

    # 5. Şəkillər: öz şəkli → əvvəlki şəkil → keş → internet axtarışı
    entry_list = list(entries.values())

    def check(e):
        return e, bool(e["imageUrl"]) and verify_image_url(e["imageUrl"], 200)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for e, ok in pool.map(check, [e for e in entry_list if e["imageUrl"]]):
            if not ok:
                e["imageUrl"] = ""
    searched = 0
    for e in sorted(entry_list, key=lambda x: x["price"]):
        if e["imageUrl"]:
            continue
        prev = prev_index.get(f"{e['key']}|{e['market']}")
        if prev and prev.get("imageUrl"):
            e["imageUrl"] = prev["imageUrl"]
        elif image_cache.get(e["key"]):
            e["imageUrl"] = image_cache[e["key"]]
        elif searched < IMAGE_SEARCH_MAX:
            e["imageUrl"] = find_high_quality_product_image(e["name"], image_cache)
            searched += 1

    # 6. Yekun siyahı
    products, drops = [], []
    for e in entry_list:
        hk = f"{e['key']}|{e['market']}"
        prev = prev_index.get(hk)
        hist = list(prev.get("history30", [])) if prev and isinstance(prev.get("history30"), list) else []
        if not hist:
            hist = [e["price"]]
        elif e.get("carried"):
            pass  # bu gün tapılmadı — tarixçəyə yeni qiymət yazılmır
        elif prev.get("histDate") == today:
            hist[-1] = e["price"]
        else:
            hist.append(e["price"])
        hist = hist[-30:]

        if prev and not e.get("carried") and safe_float(prev.get("price")) and e["price"] < safe_float(prev["price"]) - 0.005:
            drops.append({"name": e["name"], "market": e["market"], "oldPrice": safe_float(prev["price"]), "price": e["price"]})

        comps = [{"market": g["market"], "price": g["price"], "isPromo": True} for g in groups[e["key"]]]
        comps.sort(key=lambda c: c["price"])
        match = f"{len(comps)} marketdə müqayisə edildi" if len(comps) > 1 else "Bu marketin təklifidir"
        products.append({
            "id": "p_" + short_hash(hk),
            "name": e["name"], "category": e["category"], "market": e["market"], "source": e["source"],
            "oldPrice": e["oldPrice"], "price": e["price"], "unit": e["unit"], "imageUrl": e["imageUrl"],
            "endDate": e["endDate"], "svgType": pick_svg_type(e["category"], e["name"]),
            "iconBg": "from-emerald-50 to-teal-100 dark:from-emerald-950/60 dark:to-teal-900/40",
            "aiMatch": match, "comparisons": comps, "history30": hist, "histKey": hk, "histDate": today,
        })

    def discount(p):
        return (p["oldPrice"] - p["price"]) / p["oldPrice"] if p["oldPrice"] else 0

    products.sort(key=lambda p: (-discount(p), p["price"]))
    return products[:MAX_PRODUCTS], drops


# --------------------------------------------------------------------------- cashback
def find_bank_page(bank: str, domain: str) -> str:
    if DDGS is None:
        return ""
    try:
        time.sleep(0.6)
        for r in DDGS().text(f"site:{domain} market keşbek", max_results=4) or []:
            href = r.get("href") or ""
            if href.startswith("https://") and domain in urlparse(href).netloc:
                return href
    except Exception as exc:
        log.warning("  [Bank axtarışı] %s: %s", bank, exc)
    return ""


def build_cashback_sources() -> list[tuple[str, str]]:
    sources = [(b, u) for b, u, _ in CASHBACK_SOURCES]
    for bank, domain in SEARCH_BANKS:
        url = find_bank_page(bank, domain)
        if url:
            log.info("  [Cashback] %s səhifəsi tapıldı: %s", bank, url)
            sources.append((bank, url))
    return sources


def validate_cards(cards: list, source_urls: dict) -> list[dict]:
    best: dict[str, dict] = {}
    for c in cards:
        if not isinstance(c, dict) or not c.get("bank") or not c.get("cardName"):
            continue
        rate = safe_float(c.get("rate"))
        if rate is None or not (0 < rate <= 30):
            continue
        bank = clean_text(c["bank"], 40)
        card_name = clean_text(c["cardName"], 60)
        markets = c.get("markets")
        markets = [markets] if isinstance(markets, str) else markets
        if not isinstance(markets, list) or not markets:
            markets = ["Bütün marketlər"]
        conds = c.get("conditions")
        conds = [conds] if isinstance(conds, str) else conds
        grad = BANK_GRADIENTS.get(bank)
        if not grad:
            g = str(c.get("gradient") or "")
            grad = g if re.fullmatch(r"from-[\w/-]+( via-[\w/-]+)? to-[\w/-]+", g) else DEFAULT_GRADIENT
        url = str(c.get("sourceUrl") or "")
        if not url.startswith("https://"):
            url = source_urls.get(bank, "")
        card = {
            "id": "cb_" + short_hash(f"{bank}|{card_name}".lower()),
            "bank": bank, "cardName": card_name, "rate": rate,
            "rateLabel": clean_text(c.get("rateLabel") or f"{rate:g}%", 20),
            "markets": [clean_text(m, 30) for m in markets],
            "marketScope": clean_text(c.get("marketScope") or "Bütün marketlər", 80),
            "monthlyLimit": clean_text(c.get("monthlyLimit") or "", 80),
            "isSpecial": bool(c.get("isSpecial")),
            "specialTag": clean_text(c.get("specialTag") or "", 60),
            "gradient": grad,
            "conditions": [clean_text(x, 220) for x in (conds or []) if x],
            "sourceName": clean_text(c.get("sourceName") or bank, 60),
            "sourceUrl": url,
        }
        if card["id"] not in best or rate > best[card["id"]]["rate"]:
            best[card["id"]] = card
    return sorted(best.values(), key=lambda x: x["rate"], reverse=True)


def analyze_cashbacks_with_gemini(client) -> list[dict]:
    sources = build_cashback_sources()

    def grab(item):
        bank, url = item
        html = fetch_html(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return {"bank": bank, "url": url, "text": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:4500]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        raw_banks = [x for x in pool.map(grab, sources) if x]
    if not raw_banks:
        return []
    source_urls = {x["bank"]: x["url"] for x in raw_banks}

    cards = []
    for i in range(0, len(raw_banks), 4):  # kiçik partiyalar: mətn kəsilmir
        part = raw_banks[i:i + 4]
        prompt = (
            "Aşağıdakı Azərbaycan bank səhifələrindən MARKET / SUPERMARKET kateqoriyası üzrə cashback verən "
            "kartları çıxar. Heç nə uydurma; faiz mətndə yazılmayıbsa kartı daxil etmə. "
            "Qısamüddətli və ya xüsusi gün kampaniyalarında `isSpecial: true` və `specialTag` qeyd et. "
            "markets — massiv (hamısı üçün [\"Bütün marketlər\"]), conditions — qısa cümlələr massivi. "
            "YALNIZ JSON massivi qaytar. Sahələr: bank, cardName, rate (rəqəm), rateLabel, markets, marketScope, "
            "monthlyLimit, isSpecial, specialTag, conditions, sourceName, sourceUrl.\n\n"
            + json.dumps(part, ensure_ascii=False)
        )
        try:
            cards += extract_json_array(gemini_text(client, prompt))
        except Exception as exc:
            log.error("  [Cashback Gemini xəta] %s", exc)
    return validate_cards(cards, source_urls)


# --------------------------------------------------------------------------- Firestore
def init_firestore():
    if firebase_admin is None:
        raise RuntimeError("firebase-admin paketi quraşdırılmayıb.")
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT gizli açarı tapılmadı.")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(sa_json)))
    return firestore.client()


def read_doc(db, name: str) -> dict:
    if db is None:
        return {}
    try:
        snap = db.collection("catalog").document(name).get()
        return snap.to_dict() or {} if snap.exists else {}
    except Exception as exc:
        log.warning("  [Firestore oxuma] %s: %s", name, exc)
        return {}


def write_doc(db, name: str, data: dict, dry: bool):
    if dry:
        os.makedirs("out", exist_ok=True)
        with open(f"out/{name}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        log.info("  [DRY_RUN] out/%s.json yazıldı", name)
    else:
        db.collection("catalog").document(name).set(data)


def cashback_changes(old_cards: list, new_cards: list) -> list[dict]:
    old = {c.get("id"): c for c in old_cards if isinstance(c, dict)}
    changes = []
    for c in new_cards:
        o = old.get(c["id"])
        if o is None:
            changes.append({"type": "new", "bank": c["bank"], "cardName": c["cardName"], "rate": c["rate"]})
        elif safe_float(o.get("rate")) != c["rate"]:
            changes.append({"type": "rate", "bank": c["bank"], "cardName": c["cardName"],
                            "oldRate": safe_float(o.get("rate")), "rate": c["rate"]})
    return changes


# --------------------------------------------------------------------------- əsas axın
def main():
    setup_logging()
    dry = os.environ.get("DRY_RUN", "").strip() == "1"
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY gizli açarı tapılmadı.")
    if genai is None:
        raise RuntimeError("google-genai paketi quraşdırılmayıb.")
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()

    client = genai.Client(api_key=api_key)
    db = None if dry else init_firestore()
    now = datetime.now(BAKU_TZ)
    today, now_label = now.strftime("%Y-%m-%d"), now.strftime("%d.%m.%Y %H:%M")
    started = time.time()

    prev_discounts = read_doc(db, "daily_discounts")
    prev_items = [p for p in prev_discounts.get("items", []) if isinstance(p, dict)]
    prev_cashback = [c for c in read_doc(db, "daily_cashback").get("items", []) if isinstance(c, dict)]
    image_cache = dict(read_doc(db, "image_cache").get("map", {}))
    existing_logos = dict(read_doc(db, "brand_assets").get("logos", {}))

    log.info("1. Market və bank loqoları yoxlanılır...")
    write_doc(db, "brand_assets", {"updatedLabel": f"Yeniləndi: {now_label}", "logos": collect_brand_logos(existing_logos)}, dry)

    log.info("2. Market saytları skan edilir (endirim səhifələri avtomatik tapılır)...")
    web_blocks, structured, socials, tg_channels = scrape_market_websites()

    ig_blocks, ig_vision, fb_blocks, fb_vision = [], [], [], []
    if apify_token:
        log.info("3. Apify ilə Instagram və Facebook paylaşımları çəkilir...")
        ig_blocks, ig_vision = scrape_instagram_via_apify(apify_token, socials)
        fb_blocks, fb_vision = scrape_facebook_via_apify(apify_token, socials)
    else:
        log.info("3. APIFY_TOKEN tapılmadı — Instagram/Facebook addımı ötürüldü.")

    log.info("4. Telegram kanalları oxunur (%d kanal)...", len(tg_channels))
    tg_blocks, tg_vision = scrape_telegram_channels(tg_channels)

    total_sources = len(web_blocks) + len(ig_blocks) + len(fb_blocks) + len(tg_blocks) + len(structured)

    log.info("5. Vision AI və Gemini analizi aparılır...")
    vision_items = extract_from_promo_images_vision(client, ig_vision + fb_vision + tg_vision)
    text_blocks = web_blocks + ig_blocks[:40] + fb_blocks[:40] + tg_blocks
    gemini_items = gemini_extract_products(client, text_blocks) if text_blocks else []
    raw_items = structured + vision_items + gemini_items
    log.info("  Xam məhsul: strukturlu %d + Vision %d + Gemini %d", len(structured), len(vision_items), len(gemini_items))

    products, drops = build_products(raw_items, prev_items, image_cache, today)
    with_img = sum(1 for p in products if p.get("imageUrl"))
    multi = sum(1 for p in products if len(p["comparisons"]) > 1)
    log.info("Yekun məhsul: %d (şəkilli: %d, çox marketdə müqayisəli: %d, qiyməti düşən: %d)", len(products), with_img, multi, len(drops))

    if products:
        write_doc(db, "daily_discounts", {"updatedLabel": f"Yeniləndi: {now_label}", "date": today, "count": len(products), "items": products}, dry)
        write_doc(db, "image_cache", {"map": image_cache}, dry)
    else:
        log.warning("Yeni endirim tapılmadı — mövcud kataloq qorundu.")

    log.info("6. Bank cashback-ləri yenilənir...")
    cards = analyze_cashbacks_with_gemini(client)
    log.info("Yenilənən bank kartı sayı: %d", len(cards))
    cb_changes = []
    if len(cards) >= 5:
        cb_changes = cashback_changes(prev_cashback, cards) if prev_cashback else []
        write_doc(db, "daily_cashback", {"updatedLabel": f"Yeniləndi: {now_label}", "date": today, "items": cards}, dry)
    else:
        log.warning("Cashback kartı azdır (<5) — mövcud siyahı qorundu.")

    write_doc(db, "daily_changes", {
        "updatedLabel": f"Yeniləndi: {now_label}", "date": today,
        "priceDrops": drops[:60], "cashbackChanges": cb_changes[:40],
    }, dry)

    log.info("— Yekun —  vaxt: %ds | %s", int(time.time() - started), json.dumps(STATS, ensure_ascii=False))
    if total_sources == 0:
        log.error("Heç bir mənbədən məlumat alınmadı (şəbəkə və ya saytlar dəyişib).")
        sys.exit(1)


if __name__ == "__main__":
    main()
