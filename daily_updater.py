"""
Qənaət — Super Bot v4.0 (Apify Instagram + Facebook + Web + Telegram + Vision AI + Şəkil & Loqo)
Hər səhər GitHub Actions üzərindən $0/ay (Apify $5 pulsuz kredit + Gemini Free Tier) işləyir:
1. Rəsmi market saytlarından məhsulları VƏ həmin saytların rəsmi Instagram/Facebook linklərini avtomatik tapır.
2. APIFY_TOKEN varsa, 10 marketin rəsmi Instagram və Facebook səhifələrindən son 3 paylaşımı (mətn + şəkillər)
   birbaşa Apify API ilə çəkir (ayda ~$3.50 — $5 pulsuz kreditin içinə tam sığır).
3. İctimai Telegram kanallarını da paralel oxuyur.
4. Instagram/Facebook/Telegram endirim şəkillərini Gemini 2.5 Flash Vision ilə oxuyub məhsul + qiymət çıxarır.
5. Şəkli olmayan məhsulun birə-bir adını internetdə axtarıb keyfiyyətli tək məhsul şəklini tapır və keşləyir.
6. Bütün 10 marketin və bankların loqolarını `catalog/brand_assets`-də saxlayır, 30 günlük qiymət tarixçəsini
   və bank cashback siyahısını yeniləyir.
"""
import io
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
import firebase_admin
from firebase_admin import credentials, firestore
from google import genai
from google.genai import types

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

BAKU_TZ = timezone(timedelta(hours=4))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"
}

WATERMARK_DOMAINS = (
    "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    "dreamstime.com", "123rf.com", "depositphotos.com", "vecteezy.com", "freepik.com"
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

# Rəsmi saytlardan avtomatik tapılmazsa ehtiyat Instagram/Facebook ünvanları
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



GEMINI_MODELS = ["gemini-3.0-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-3-flash-preview"]


def discover_available_models(client) -> list[str]:
    """API açarında aktiv olan Flash modellərini avtomatik tapır."""
    found = []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            if "flash" in name.lower() and "embedding" not in name.lower() and "audio" not in name.lower():
                found.append(name)
    except Exception:
        pass
    ordered = [m for m in GEMINI_MODELS if m in found] + [m for m in found if m not in GEMINI_MODELS]
    return ordered[:4] if ordered else GEMINI_MODELS


ACTIVE_MODELS = None


def gemini_generate(client, contents):
    global ACTIVE_MODELS
    if ACTIVE_MODELS is None:
        ACTIVE_MODELS = discover_available_models(client)
        print(f"  [Gemini] Aktiv modellər: {ACTIVE_MODELS}")
    last_err = None
    for model_name in ACTIVE_MODELS:
        for attempt in range(2):
            try:
                time.sleep(2.5)  # Free tier rate limit / 503 qorunması
                return client.models.generate_content(model=model_name, contents=contents)
            except Exception as exc:
                last_err = exc
                msg = str(exc)
                if "404" in msg or "NOT_FOUND" in msg:
                    break
                if any(k in msg for k in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")):
                    wait_s = 8 * (attempt + 1)
                    print(f"  [Gemini {model_name}] yüklənmə ({attempt+1}/2), {wait_s}s gözlənilir...")
                    time.sleep(wait_s)
                    continue
                raise
    raise last_err


def fetch_html(url: str, timeout: int = 16) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception as exc:
        print(f"  [Veb xəta] {url}: {exc}")
    return ""


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9əöüğçşı]+", "", (name or "").lower())[:40]


def extract_json_array(raw_text: str) -> list:
    match = re.search(r"\[.*\]", raw_text or "", re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except Exception:
        return []


def verify_image_url(url: str, min_dim: int = 220) -> bool:
    if not url or not url.startswith("https://"):
        return False
    host = urlparse(url).netloc.lower()
    if any(wm in host for wm in WATERMARK_DOMAINS):
        return False
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, stream=True)
        if r.status_code != 200:
            return False
        if "image" not in (r.headers.get("Content-Type") or "").lower():
            return False
        chunk = r.raw.read(65536)
        im = Image.open(io.BytesIO(chunk))
        w, h = im.size
        if w < min_dim or h < min_dim:
            return False
        ratio = w / float(h)
        return 0.45 <= ratio <= 2.1
    except Exception:
        return False


def find_high_quality_product_image(product_name: str, image_cache: dict) -> str:
    key = normalize_key(product_name)
    if key in image_cache and image_cache[key]:
        return image_cache[key]
    if DDGS is None:
        return ""
    for q in [f'"{product_name}"', f"{product_name} product"]:
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
            print(f"  [Şəkil axtarış xətası] {product_name}: {exc}")
            break
    return ""


def collect_brand_logos(existing_logos: dict) -> dict:
    logos = dict(existing_logos or {})
    brands = [(m, dom) for m, _, dom in MARKET_WEBSITES] + [
        (b, dom) for b, _, dom in CASHBACK_SOURCES if b != "FED.az"
    ]
    for name, domain in brands:
        if logos.get(name) and logos[name].startswith("https://"):
            continue
        found = ""
        html = fetch_html(f"https://{domain}", timeout=10)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for sel, attr in [('link[rel="apple-touch-icon"]', "href"), ('meta[property="og:image"]', "content")]:
                tag = soup.select_one(sel)
                if tag and tag.get(attr):
                    cand = urljoin(f"https://{domain}", tag.get(attr))
                    if cand.startswith("https://"):
                        found = cand
                        break
        if not found:
            found = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        logos[name] = found
    return logos


def scrape_market_websites_and_socials() -> tuple[list[dict], dict]:
    """Market saytlarını skan edir və onların rəsmi Instagram/Facebook linklərini çıxarır."""
    blocks = []
    discovered_socials = {k: dict(v) for k, v in DEFAULT_SOCIAL_URLS.items()}

    for market, url, _ in MARKET_WEBSITES:
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        # Saytın özündən rəsmi Instagram və Facebook linklərini avtomatik tap
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            low = href.lower()
            if "instagram.com/" in low and "/p/" not in low and "/reel/" not in low:
                discovered_socials.setdefault(market, {})["ig"] = href.split("?")[0]
            elif "facebook.com/" in low and "sharer" not in low and "/posts/" not in low:
                discovered_socials.setdefault(market, {})["fb"] = href.split("?")[0]

        for tag in soup(["script", "style", "noscript", "svg", "footer", "nav"]):
            tag.decompose()

        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            alt = (img.get("alt") or "").strip()
            if not src or src.startswith("data:"):
                continue
            full_src = urljoin(url, src)
            low = full_src.lower()
            if any(skip in low for skip in ["logo", "icon", "favicon", "flag", "sprite", "avatar", "banner"]):
                continue
            if any(ext in low for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                images.append({"alt": alt[:80], "url": full_src})

        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        if len(text) > 120:
            blocks.append({
                "market": market, "source": "Veb sayt", "url": url,
                "text": text[:2800], "images": images[:20],
            })
            print(f"  [Veb] {market}: {len(text)} simvol, {len(images)} şəkil")
    return blocks, discovered_socials


def scrape_instagram_via_apify(token: str, socials: dict) -> tuple[list[dict], list[dict]]:
    """Apify Instagram Scraper ilə 10 marketin son 3 paylaşımını (mətn + şəkil) birbaşa çəkir."""
    ig_urls = []
    url_to_market = {}
    for market, links in socials.items():
        ig = links.get("ig")
        if ig and "instagram.com/" in ig:
            ig_urls.append(ig)
            handle = urlparse(ig).path.strip("/").split("/")[0].lower()
            if handle:
                url_to_market[handle] = market

    if not ig_urls:
        return [], []

    print(f"  [Apify Instagram] {len(ig_urls)} rəsmi hesab yoxlanılır (hərəsindən son 3 paylaşım)...")
    endpoint = f"https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?token={token}"
    payload = {
        "directUrls": ig_urls[:10],
        "resultsType": "posts",
        "resultsLimit": 3,
    }
    blocks = []
    vision_candidates = []
    try:
        r = requests.post(endpoint, json=payload, timeout=180)
        if r.status_code not in (200, 201):
            print(f"  [Apify Instagram xəta] HTTP {r.status_code}: {r.text[:200]}")
            return [], []
        posts = r.json() if isinstance(r.json(), list) else []
        print(f"  [Apify Instagram] {len(posts)} paylaşım alındı")
        for post in posts:
            owner = (post.get("ownerUsername") or "").lower()
            market = url_to_market.get(owner, owner or "Instagram")
            caption = (post.get("caption") or "").strip()
            img_url = post.get("displayUrl") or ""
            if caption:
                blocks.append({
                    "market": market, "source": "Instagram",
                    "text": caption[:600], "imageUrl": img_url,
                })
            # Endirim sözü varsa və ya qısa mətndirsə şəklini Vision AI-a göndər
            if img_url and len(vision_candidates) < 8:
                if any(k in caption.lower() for k in ["endirim", "aksiya", "azn", "₼", "super", "qiymət", "%"]) or len(caption) < 60:
                    vision_candidates.append({"market": market, "source": "Instagram (Vision AI)", "imageUrl": img_url})
    except Exception as exc:
        print(f"  [Apify Instagram xəta] {exc}")
    return blocks, vision_candidates


def scrape_facebook_via_apify(token: str, socials: dict) -> tuple[list[dict], list[dict]]:
    """Apify Facebook Page Posts Scraper ilə marketlərin rəsmi Facebook səhifələrindən son 3 paylaşımı çəkir."""
    fb_urls = []
    for market, links in socials.items():
        fb = links.get("fb")
        if fb and "facebook.com/" in fb:
            fb_urls.append(fb)

    if not fb_urls:
        return [], []

    print(f"  [Apify Facebook] {len(fb_urls)} rəsmi səhifə yoxlanılır (hərəsindən son 3 paylaşım)...")
    endpoint = f"https://api.apify.com/v2/acts/automly~facebook-page-posts-scraper/run-sync-get-dataset-items?token={token}"
    payload = {
        "pages": fb_urls[:10],
        "maxPosts": 3,
    }
    blocks = []
    vision_candidates = []
    try:
        r = requests.post(endpoint, json=payload, timeout=180)
        if r.status_code not in (200, 201):
            print(f"  [Apify Facebook xəta] HTTP {r.status_code}: {r.text[:200]}")
            return [], []
        posts = r.json() if isinstance(r.json(), list) else []
        print(f"  [Apify Facebook] {len(posts)} paylaşım alındı")
        for post in posts:
            page_name = post.get("pageName") or post.get("user", {}).get("name") or "Facebook"
            text = (post.get("text") or post.get("postText") or "").strip()
            media = post.get("media") or []
            img_url = ""
            if isinstance(media, list) and media:
                first = media[0]
                img_url = first.get("thumbnail") or first.get("photo_image", {}).get("uri") or first.get("url") or ""
            if text:
                blocks.append({
                    "market": page_name, "source": "Facebook",
                    "text": text[:600], "imageUrl": img_url,
                })
            if img_url and len(vision_candidates) < 6:
                vision_candidates.append({"market": page_name, "source": "Facebook (Vision AI)", "imageUrl": img_url})
    except Exception as exc:
        print(f"  [Apify Facebook xəta] {exc}")
    return blocks, vision_candidates


def scrape_telegram_channels() -> tuple[list[dict], list[dict]]:
    blocks = []
    vision_candidates = []
    for market, slug in TELEGRAM_CHANNELS.items():
        html = fetch_html(f"https://t.me/s/{slug}", timeout=15)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        posts = []
        for wrap in soup.select(".tgme_widget_message_wrap")[-6:]:
            txt_el = wrap.select_one(".tgme_widget_message_text")
            text = txt_el.get_text(" ", strip=True) if txt_el else ""
            img_url = ""
            photo_el = wrap.select_one(".tgme_widget_message_photo_wrap")
            if photo_el and photo_el.get("style"):
                m = re.search(r"url\(['\"]?(https?://[^'\")]+)", photo_el["style"])
                if m:
                    img_url = m.group(1)
            if text and len(text) > 15:
                posts.append({"text": text[:400], "imageUrl": img_url})
            elif img_url and len(vision_candidates) < 4:
                vision_candidates.append({"market": market, "source": "Telegram (Vision AI)", "imageUrl": img_url})
        if posts:
            blocks.append({"market": market, "source": "Telegram kanalı", "posts": posts})
    return blocks, vision_candidates


def extract_from_promo_images_vision(client, candidates: list[dict]) -> list[dict]:
    """Instagram, Facebook və Telegram promosyon şəkillərini Gemini 2.5 Flash Vision ilə oxuyur."""
    extracted = []
    for cand in candidates[:4]:
        try:
            r = requests.get(cand["imageUrl"], headers=HEADERS, timeout=12)
            if r.status_code != 200 or len(r.content) > 4_500_000:
                continue
            part = types.Part.from_bytes(data=r.content, mime_type="image/jpeg")
            prompt = (
                f"Bu {cand['market']} marketinin sosial şəbəkə endirim paylaşımıdır. "
                "Şəkildə konkret qiyməti (AZN / ₼) görünən bütün məhsulları YALNIZ JSON massivi kimi qaytar: "
                '[{"name": "...", "category": "Ərzaq", "oldPrice": 3.5, "price": 2.49, "unit": "1 ədəd"}]. '
                "Əgər qiymət yoxdursa [] qaytar."
            )
            resp = gemini_generate(client, [prompt, part])
            for item in extract_json_array(resp.text):
                if item.get("name") and item.get("price"):
                    item["market"] = cand["market"]
                    item["source"] = cand.get("source", "Sosial şəbəkə (Vision AI)")
                    extracted.append(item)
        except Exception as exc:
            print(f"  [Vision xətası] {cand['market']}: {exc}")
    if extracted:
        print(f"  [Vision AI] Sosial şəbəkə şəkillərindən {len(extracted)} məhsul oxundu")
    return extracted


def pick_svg_type(category: str, name: str) -> str:
    low = (name + " " + category).lower()
    if any(k in low for k in ["süd", "sud", "qatıq", "qatiq", "pendir"]):
        return "milk"
    if any(k in low for k in ["yağ", "yag", "kərə", "kere"]):
        return "oil"
    if any(k in low for k in ["çay", "cay", "qəhvə", "kofe"]):
        return "tea"
    if any(k in low for k in ["yuyucu", "ariel", "persil", "təmiz", "fairy", "sabun"]):
        return "detergent"
    if any(k in low for k in ["kola", "cola", "su", "şirə", "içki", "fanta"]):
        return "drink"
    return "milk"


def analyze_discounts_with_gemini(client, raw_blocks: list[dict], vision_items: list[dict], existing_history: dict, image_cache: dict) -> list[dict]:
    if not raw_blocks and not vision_items:
        return []
    payload = {"sources": raw_blocks, "vision_extracted": vision_items}
    prompt = (
        "Sən Azərbaycan supermarket endirimlərini analiz edən peşəkar AI sistemsən.\n"
        "Aşağıdakı Instagram, Facebook, Telegram, veb sayt və Vision AI məlumatlarından REAL qiyməti (AZN) "
        "olan endirimli məhsulları çıxar.\n"
        "Qaydalar:\n"
        "1. Yalnız real məhsul adı və rəqəmsal qiyməti olanları götür (uydurma qiymət yazma).\n"
        "2. Market adını standartlaşdır: Bravo, Araz, Bazarstore, Rahat, OBA, AL Market, Neptun, Grandmart, Spar, Bolmart.\n"
        "3. Eyni məhsul bir neçə marketdə varsa, bir məhsulda birləşdirib `comparisons` massivinə yaz.\n"
        "4. Toplu kataloq/post şəkillərini `imageUrl`-ə QOYMA (boş burax ki, bot tək məhsul şəklini internetdə tapsın); "
        "yalnız veb saytdan gələn tək məhsul şəklini qoy.\n"
        "5. YALNIZ JSON massivi qaytar (maksimum 40 məhsul). Sahələr:\n"
        '   name, category ("Süd məhsulları"|"Ərzaq"|"İçkilər"|"Təmizlik"|"Şirniyyat"), market, '
        '   source ("Instagram"|"Facebook"|"Veb sayt"|"Telegram kanalı"), oldPrice, price, unit, imageUrl, '
        '   aiMatch, comparisons ([{"market": "...", "price": 1.99, "isPromo": true}]).\n\n'
        + json.dumps(payload, ensure_ascii=False)[:16000]
    )
    try:
        resp = gemini_generate(client, prompt)
        items = extract_json_array(resp.text)
        if not items and vision_items:
            items = list(vision_items[:35])
    except Exception as exc:
        print(f"Gemini birləşdirmə xətası (Vision məhsulları qorunur): {exc}")
        items = list(vision_items[:35]) if vision_items else []

    cleaned = []
    searched_images = 0
    for idx, it in enumerate(items):
        try:
            price = round(float(it["price"]), 2)
            if price <= 0 or not it.get("name"):
                continue
        except Exception:
            continue
        old_price = round(float(it.get("oldPrice") or round(price * 1.15, 2)), 2)
        if old_price < price:
            old_price = price

        name = str(it["name"]).strip()[:95]
        key = normalize_key(name)

        img_url = str(it.get("imageUrl") or "").strip()
        if img_url and not verify_image_url(img_url, min_dim=200):
            img_url = ""
        if not img_url and searched_images < 20:
            img_url = find_high_quality_product_image(name, image_cache)
            searched_images += 1

        hist = list(existing_history.get(key, []))
        if not hist:
            hist = [old_price, price] if old_price > price else [price]
        elif hist[-1] != price:
            hist.append(price)
        hist = hist[-30:]

        comps = it.get("comparisons")
        if not isinstance(comps, list) or not comps:
            comps = [{"market": it.get("market", "Bravo"), "price": price, "isPromo": True}]

        cat = it.get("category", "Ərzaq")
        cleaned.append({
            "id": f"p_{key[:12]}_{idx+1}",
            "name": name,
            "category": cat,
            "market": str(it.get("market", "Bravo")).strip(),
            "source": str(it.get("source", "Instagram")).strip(),
            "oldPrice": old_price,
            "price": price,
            "unit": str(it.get("unit", "1 ədəd")).strip()[:25],
            "imageUrl": img_url,
            "svgType": pick_svg_type(cat, name),
            "iconBg": "from-emerald-50 to-teal-100 dark:from-emerald-950/60 dark:to-teal-900/40",
            "aiMatch": str(it.get("aiMatch") or "AI avtomatik uyğunlaşdırma")[:60],
            "comparisons": comps,
            "history30": hist,
        })
    return cleaned


def analyze_cashbacks_with_gemini(client) -> list[dict]:
    raw_banks = []
    for bank, url, _ in CASHBACK_SOURCES:
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:3500]
        raw_banks.append({"bank": bank, "url": url, "text": txt})
    if not raw_banks:
        return []

    prompt = (
        "Aşağıdakı Azərbaycan bank səhifələrindən MARKET / SUPERMARKET kateqoriyası üzrə cashback verən "
        "kartları çıxar və faizə görə çoxdan aza sıralı YALNIZ JSON massivi qaytar. "
        "Qısamüddətli və ya xüsusi gün kampaniyalarında `isSpecial: true` və `specialTag` qeyd et.\n"
        "Sahələr: id, bank, cardName, rate, rateLabel, markets, marketScope, monthlyLimit, "
        "isSpecial, specialTag, gradient, conditions, sourceName, sourceUrl.\n\n"
        + json.dumps(raw_banks, ensure_ascii=False)[:24000]
    )
    try:
        resp = gemini_generate(client, prompt)
        cards = extract_json_array(resp.text)
        valid = [c for c in cards if c.get("bank") and c.get("cardName") and c.get("rate") is not None]
        valid.sort(key=lambda x: float(x.get("rate", 0)), reverse=True)
        return valid
    except Exception as exc:
        print(f"Gemini cashback xətası: {exc}")
        return []


def init_firestore():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT gizli açarı tapılmadı.")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(sa_json)))
    return firestore.client()


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY gizli açarı tapılmadı.")
    apify_token = os.environ.get("APIFY_TOKEN", "").strip()

    client = genai.Client(api_key=api_key)
    db = init_firestore()
    now_baku = datetime.now(BAKU_TZ).strftime("%d.%m.%Y %H:%M")

    # 1. Mövcud tarixçə, şəkil keşi və loqoları oxu
    existing_history = {}
    prev_doc = db.collection("catalog").document("daily_discounts").get()
    if prev_doc.exists:
        for item in prev_doc.to_dict().get("items", []):
            k = normalize_key(item.get("name", ""))
            if k and isinstance(item.get("history30"), list):
                existing_history[k] = item["history30"]

    cache_doc = db.collection("catalog").document("image_cache").get()
    image_cache = dict(cache_doc.to_dict().get("map", {})) if cache_doc.exists else {}

    assets_doc = db.collection("catalog").document("brand_assets").get()
    existing_logos = dict(assets_doc.to_dict().get("logos", {})) if assets_doc.exists else {}

    # 2. Loqolar
    print("1. Market və bank loqoları yoxlanılır...")
    logos = collect_brand_logos(existing_logos)
    db.collection("catalog").document("brand_assets").set({
        "updatedLabel": f"Yeniləndi: {now_baku}",
        "logos": logos,
    })

    # 3. Veb saytlar + rəsmi sosial linklərin avtomatik kəşfi
    print("2. Market saytları skan edilir...")
    web_blocks, socials = scrape_market_websites_and_socials()

    # 4. Apify Instagram + Facebook (APIFY_TOKEN varsa)
    ig_blocks, ig_vision = ([], [])
    fb_blocks, fb_vision = ([], [])
    if apify_token:
        print("3. Apify ilə Instagram və Facebook paylaşımları çəkilir...")
        ig_blocks, ig_vision = scrape_instagram_via_apify(apify_token, socials)
        fb_blocks, fb_vision = scrape_facebook_via_apify(apify_token, socials)
    else:
        print("3. APIFY_TOKEN tapılmadı — Instagram/Facebook addımı ötürüldü.")

    # 5. Telegram + Vision AI
    print("4. Telegram və Vision AI analizi aparılır...")
    tg_blocks, tg_vision = scrape_telegram_channels()
    vision_items = extract_from_promo_images_vision(client, ig_vision + fb_vision + tg_vision)

    raw_blocks = web_blocks + ig_blocks[:25] + fb_blocks[:25] + tg_blocks
    products = analyze_discounts_with_gemini(client, raw_blocks, vision_items, existing_history, image_cache)
    with_img = sum(1 for p in products if p.get("imageUrl"))
    print(f"Tapılan endirimli məhsul: {len(products)} (tək məhsul şəkilli: {with_img})")

    if products:
        db.collection("catalog").document("daily_discounts").set({
            "updatedLabel": f"Yeniləndi: {now_baku}",
            "items": products,
        })
        db.collection("catalog").document("image_cache").set({"map": image_cache})
        print("Firestore catalog/daily_discounts və image_cache yeniləndi.")
    else:
        print("Yeni endirim tapılmadı — mövcud kataloq qorundu.")

    # 6. Bank cashback yeniləməsi
    print("5. Bank cashback-ləri yenilənir...")
    cards = analyze_cashbacks_with_gemini(client)
    print(f"Yenilənən bank kartı sayı: {len(cards)}")
    if len(cards) >= 5:
        db.collection("catalog").document("daily_cashback").set({
            "updatedLabel": f"Yeniləndi: {now_baku}",
            "items": cards,
        })
        print("Firestore catalog/daily_cashback yeniləndi.")


if __name__ == "__main__":
    main()
