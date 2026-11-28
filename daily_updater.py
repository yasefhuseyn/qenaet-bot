"""
Qənaət — Super Bot v6.0
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
  BAZAR_MAX_PRODUCTS=240   Bazarstore endirim səhifələrindən maksimum məhsul
  USE_APIFY=0              Apify addımını söndürür (xərc yoxdur)
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
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from firebase_admin import messaging
    except ImportError:
        messaging = None
except ImportError:  # DRY_RUN üçün Firebase lazım deyil
    firebase_admin = credentials = firestore = messaging = None

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
for _n in ("google_genai", "google_genai.models", "google.genai", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

BAKU_TZ = timezone(timedelta(hours=4))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36",
    "Accept-Language": "az,en;q=0.8,ru;q=0.6",
}


TG_ONLY = os.environ.get("TELEGRAM_ONLY", "1").strip() != "0"   # yalnız Telegram/Instagram/Facebook


def env_int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


IG_POSTS = env_int("IG_POSTS_PER_PAGE", 10)
FB_POSTS = env_int("FB_POSTS_PER_PAGE", 12)
TG_PAGES = env_int("TG_PAGES_PER_CHANNEL", 25)   # hər səhifədə ~20 paylaşım
TG_POSTS = env_int("TG_POSTS_PER_CHANNEL", 30)
VISION_MAX = env_int("VISION_MAX_IMAGES", 400)
VISION_PER_CALL = 4        # bir Gemini sorğusunda neçə şəkil oxunsun
VISION_PER_MARKET = 10     # hər saytdan maksimum promo şəkli
BAZAR_MAX = env_int("BAZAR_MAX_PRODUCTS", 240)
EXTRA_PAGES = env_int("EXTRA_PAGES_PER_MARKET", 3)
IMAGE_SEARCH_MAX = env_int("IMAGE_SEARCH_MAX", 350)
CATALOG_MAX_IMAGES = env_int("CATALOG_MAX_IMAGES", 60)     # bir işləmədə kəsilən kataloq şəkli sayı
CATALOG_MAX_CROPS = env_int("CATALOG_MAX_CROPS", 250)       # bir işləmədə kəsilən məhsul şəkli sayı
CROP_DIR = "product_images"
CROP_RAW_BASE = "https://raw.githubusercontent.com/yasefhuseyn/qenaet-bot/main/" + CROP_DIR + "/"
CATALOG_IMAGES: list[dict] = []   # vision zamanı "kataloq" kimi tanınan şəkillər
MAX_PRODUCTS = 600
MAX_AGE_DAYS = 21          # bundan köhnə sosial şəbəkə paylaşımları atılır
MIN_PRICE, MAX_PRICE = 0.05, 300.0
GEMINI_BATCH_CHARS = 22000
GEMINI_MAX_BATCHES = 4
GEMINI_MIN_INTERVAL = 4.0  # saniyə (pulsuz limitə uyğun)
LOGO_VERSION = 4           # dəyişəndə bütün loqolar yenidən yoxlanılır
IMAGE_CACHE_VERSION = 3    # dəyişəndə köhnə (yanlış ola bilən) şəkil keşi silinir
BAZAR_LOOKUP_MAX = env_int("BAZAR_LOOKUP_MAX", 300)
VERIFY_MAX = env_int("VISION_VERIFY_MAX", 250)   # axtarışla tapılan şəkilləri AI ilə yoxlama limiti
NEG_CACHE_DAYS = 2        # uyğun şəkil tapılmayan məhsul bu müddət təkrar axtarılmır
_verify_used = 0
IMAGE_BUDGET_SEC = env_int("IMAGE_BUDGET_SEC", 2400)   # şəkil axtarışı üçün ümumi vaxt limiti
_img_started = 0.0

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
    ("Grandmart", "https://www.grandmartsupermarket.com/", "grandmartsupermarket.com"),
    ("Bolmart", "https://www.bolmart.az/", "bolmart.az"),
    ("Tamstore", "https://tamstore.az/", "tamstore.az"),
]

# Rəsmi saytlardan avtomatik tapılmazsa ehtiyat ünvanlar
DEFAULT_SOCIAL_URLS = {   # istifadəçinin verdiyi rəsmi ünvanlar (2026-11-28)
    "Bravo":      {"ig": "https://www.instagram.com/bravosupermarketaz/", "fb": "https://www.facebook.com/bravosupermarketaz/"},
    "Araz":       {"ig": "https://www.instagram.com/arazsupermarket/", "fb": "https://www.facebook.com/arazsupermarket/"},
    "Bazarstore": {"ig": "https://www.instagram.com/bazarstore_supermarket/", "fb": "https://www.facebook.com/bazarstoresupermarket/"},
    "OBA":        {"ig": "https://www.instagram.com/obamarketler/", "fb": "https://www.facebook.com/obamarket.az/"},
    "AL Market":  {"ig": "https://www.instagram.com/almarketler/", "fb": "https://www.facebook.com/almarketler/"},
    "Rahat":      {"ig": "https://www.instagram.com/rahatmarket.az/", "fb": "https://www.facebook.com/rahatmarket.az/"},
    "Bolmart":    {"ig": "https://www.instagram.com/bolmart.supermarket/", "fb": "https://www.facebook.com/bolmart.supermarket/"},
    "Grandmart":  {"ig": "https://www.instagram.com/grandmart.supermarket/", "fb": "https://www.facebook.com/grandmart.supermarket/"},
    "Tamstore":   {"ig": "https://www.instagram.com/tamstore.az/", "fb": "https://www.facebook.com/tamstoremarket/"},
}

# Facebook səhifələri (rəsmi, 2026-11-26 yoxlanılıb) — saytdan tapılandan üstündür
VERIFIED_FB = {m: v["fb"] for m, v in DEFAULT_SOCIAL_URLS.items()}

TELEGRAM_CHANNELS = {   # t.me/s/ ilə açılışı yoxlanılıb (2026-11-26)
    "Bravo": "bravosupermarketaze",
    "Araz": "arazsupermarket",
    "OBA": "obamarketlersebekesi",
    "AL Market": "almarketler",
    "Bazarstore": "bazarstoretelegramkanali",
    "Rahat": "rahatmarketaz",
    "Bolmart": "bolmartsupermarket",
    "Grandmart": "grandmartsupermarket",
    "Tamstore": "tamstore_az",
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

EXCLUDED_MARKETS = {"Spar", "Neptun"}
CANONICAL_MARKETS = ["Bravo", "Araz", "Bazarstore", "Rahat", "OBA", "AL Market", "Grandmart", "Bolmart", "Tamstore"]
MARKET_ALIASES = {
    "bravo": "Bravo", "araz": "Araz", "bazar": "Bazarstore", "rahat": "Rahat", "oba": "OBA",
    "almarket": "AL Market", "al market": "AL Market", "al-market": "AL Market",
    "grandmart": "Grandmart", "grand mart": "Grandmart", "bolmart": "Bolmart", "bol mart": "Bolmart",
}
ALLOWED_CATEGORIES = ("Süd məhsulları", "Ət və quş əti", "Balıq və dəniz məhsulları", "Meyvə və tərəvəz", "Çörək və un məmulatları", "Bakaliya", "İçkilər", "Şirniyyat",
                      "Qəlyanaltı və qoz-fındıq", "Dondurulmuş məhsullar", "Təmizlik və məişət kimyası", "Şəxsi gigiyena və gözəllik", "Uşaq məhsulları", "Heyvan yemi", "Ev və mətbəx", "Digər")
PROMO_KEYWORDS = ("endirim", "aksiya", "kampaniya", "special", "promo", "offer", "discount", "kataloq",
                  "catalog", "teklif", "təklif", "sale", "kampaniy")
PRICE_RE = re.compile(r"\d+(?:[.,]\d{1,2})?\s*(?:₼|azn|man\b|manat)", re.I)

# Köhnə model adları 404 verir; "-latest" ləqəbləri həmişə cari modelə işarə edir
GEMINI_MODELS = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.5-flash-lite",
                 "gemini-flash-lite-latest", "gemini-3-flash-preview"]
_dead_models: set = set()      # bu işləmədə 404/kvota bitmiş modellər təkrar yoxlanılmır
_cool_until: dict = {}         # həddən artıq yüklənmiş modellər müvəqqəti gözləyir

STATS = {"bazar": 0, "araz": 0, "cards": 0, "image_verified": 0, "web_pages": 0, "structured": 0, "ig_posts": 0, "fb_posts": 0, "tg_posts": 0,
         "vision_images": 0, "gemini_calls": 0, "gemini_errors": 0, "image_searches": 0, "brand_verified": 0}


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


def with_retry(fn, label: str, tries: int = 3, delay: int = 15, is_empty=None, default=None):
    """Müvəqqəti xəta və ya boş cavab olanda təkrar cəhd edir; sonda ən yaxşı nəticəni qaytarır."""
    is_empty = is_empty or (lambda r: not r)
    res = default
    for i in range(1, tries + 1):
        try:
            res = fn()
            if not is_empty(res):
                if i > 1:
                    log.info("  [Təkrar cəhd] %s: %d-ci cəhddə alındı", label, i)
                return res
            log.warning("  [Boş cavab] %s (cəhd %d/%d)", label, i, tries)
        except Exception as exc:
            log.warning("  [Xəta] %s (cəhd %d/%d): %s", label, i, tries, str(exc)[:120])
        if i < tries:
            time.sleep(delay * i)
    return res


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


def pick_models(client):
    """Hesabda həqiqətən mövcud olan modelləri seçir (köhnə ad 404 verməsin)."""
    global GEMINI_MODELS
    try:
        avail = set()
        for m in client.models.list():
            acts = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            if "generateContent" in acts:
                avail.add(m.name.replace("models/", ""))
        chosen = [m for m in GEMINI_MODELS if m in avail]
        extra = sorted((m for m in avail if re.fullmatch(r"gemini-[\d.]+-flash(-lite)?", m) and m not in chosen), reverse=True)
        GEMINI_MODELS = (chosen + extra)[:6] or GEMINI_MODELS
        log.info("  [Gemini] istifadə olunacaq modellər: %s", ", ".join(GEMINI_MODELS))
    except Exception as exc:
        log.warning("  [Gemini] model siyahısı alınmadı (%s), standart siyahı işlədilir", type(exc).__name__)


_RUN_START = time.time()
GEMINI_DEADLINE_SEC = env_int("GEMINI_DEADLINE_SEC", 2000)   # bu vaxtdan sonra Gemini çağırılmır (iş yarımçıq qalmasın)


def gemini_text(client, contents) -> str:
    """Gemini-yə sorğu: işləməyən modeli atır, yüklənmiş modeli gözlədir, növbəti modelə keçir."""
    global _last_gemini_call
    if time.time() - _RUN_START > GEMINI_DEADLINE_SEC:
        raise RuntimeError("Gemini vaxt limiti keçdi")
    config = None
    if types is not None:
        try:
            config = types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.1,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        except Exception:
            try:
                config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
            except Exception:
                config = None
    last_err = None
    for _round in range(3):
        usable = [m for m in GEMINI_MODELS if m not in _dead_models]
        if not usable:
            break
        for model_name in usable:
            if _cool_until.get(model_name, 0) > time.time():
                continue
            for attempt in range(2):
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
                    if any(k in msg for k in ("404", "NOT_FOUND", "403", "PERMISSION_DENIED")):
                        _dead_models.add(model_name)
                        log.warning("  [Gemini] %s mövcud deyil/icazə yoxdur — bu işləmədə atılır", model_name)
                        break
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        if attempt == 1 or re.search(r"per.?day|PerDay", msg, re.I):
                            _dead_models.add(model_name)
                            log.warning("  [Gemini] %s kvotası bitdi — növbəti modelə keçilir", model_name)
                            break
                        time.sleep(12)
                        continue
                    if any(k in msg for k in ("503", "UNAVAILABLE", "overloaded", "500", "INTERNAL", "DEADLINE")):
                        time.sleep(4 * (attempt + 1))
                        if attempt == 1:
                            _cool_until[model_name] = time.time() + 90
                            log.warning("  [Gemini] %s yüklənib — 90s kənara qoyuldu", model_name)
                        continue
                    STATS["gemini_errors"] += 1
                    raise
        cooling = [t for m, t in _cool_until.items() if m not in _dead_models and t > time.time()]
        if not cooling:
            break
        time.sleep(min(max(cooling) - time.time(), 60) + 1)
    STATS["gemini_errors"] += 1
    raise last_err or RuntimeError("Gemini cavab vermədi (bütün modellər əlçatmazdır)")


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


def name_tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9əöüğçşı]{3,}", (text or "").lower())
            if t not in ("the", "and", "kq", "qr", "ədədli", "edədli", "endirimli")}


def tokens_overlap(a: str, b: str, need: int = 1) -> bool:
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta & tb) >= min(need, len(ta) or need)


def extract_json_object(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def vision_confirms_product(client, image_url: str, product_name: str) -> bool:
    """Gemini şəkilə baxıb bunun həqiqətən həmin məhsulun təmiz şəkli olub-olmadığını deyir."""
    global _verify_used
    if client is None or types is None or _verify_used >= VERIFY_MAX:
        return None   # yoxlama mümkün deyil (kvota/limit)
    try:
        r = SESSION.get(image_url, timeout=10)
        if r.status_code != 200 or len(r.content) > 3_000_000:
            return False
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            return False
        _verify_used += 1
        STATS["image_verified"] += 1
        prompt = (
            f'Bu şəkil "{product_name}" məhsulunun təmiz, tək məhsul şəklidirmi? '
            "Marka və məhsul növü uyğun olmalıdır. Şəxs, musiqiçi, oyun/film qrafikası, loqo, "
            "bir neçə fərqli məhsulun kollajı, mətbuat/kataloq səhifəsi və ya başqa məhsul olarsa uyğun DEYİL. "
            'YALNIZ JSON qaytar: {"match": true} və ya {"match": false}'
        )
        resp = gemini_text(client, [prompt, types.Part.from_bytes(data=r.content, mime_type=mime)])
        obj = extract_json_object(resp)
        if "match" not in obj:
            return None
        return obj.get("match") is True
    except Exception as exc:
        log.warning("  [Şəkil yoxlaması] %s: %s", product_name, exc)
        return None


MARKET_IMAGE_DOMAINS = ("almarket.az", "bazarstore.az", "arazmarket.az", "oba.az", "obafiles", "bravosupermarket.az",
                        "rahatmarket.az", "neptun.az", "bolmart.az", "grandmart", "azersun", "sima.az", "kontakt.az")


def clean_query_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b(kq|st|bah|lim)\b", " ", name, flags=re.I)).strip()


def find_high_quality_product_image(product_name: str, image_cache: dict, client=None, today: str = "") -> str:
    """İnternetdə (Google/Bing) məhsulun adı ilə axtarır, AMMA yalnız Gemini təsdiq etdiyi şəkli qəbul edir."""
    key = normalize_key(product_name)
    cached = image_cache.get(key) or ""
    if cached.startswith("https://"):
        return cached
    if cached.startswith("none:") and today:
        try:
            if (datetime.fromisoformat(today) - datetime.fromisoformat(cached[5:])).days < NEG_CACHE_DAYS:
                return ""
        except ValueError:
            pass
    global _img_started
    if not _img_started:
        _img_started = time.time()
    if DDGS is None or time.time() - _img_started > IMAGE_BUDGET_SEC:
        return ""
    STATS["image_searches"] += 1
    need = 2 if len(name_tokens(product_name)) >= 2 else 1
    qname = clean_query_name(product_name)
    scored = {}
    for q in (qname,):
        for backend in ("bing",):
            try:
                time.sleep(0.5)
                results = DDGS().images(q, max_results=10, backend=backend) or []
            except Exception as exc:
                log.debug("  [Şəkil axtarış] %s/%s: %s", backend, q, type(exc).__name__)
                continue
            for item in results:
                img_url = item.get("image") or ""
                w, h = int(item.get("width") or 0), int(item.get("height") or 0)
                if not img_url.startswith("https://") or img_url in scored or (w and h and (w < 300 or h < 300)):
                    continue
                overlap = len(name_tokens(product_name) & name_tokens((item.get("title") or "") + " " + img_url))
                if overlap < min(need, len(name_tokens(product_name)) or need):
                    continue
                host = urlparse(img_url).netloc.lower()
                boost = 3 if any(d in host or d in img_url for d in MARKET_IMAGE_DOMAINS) else (1 if host.endswith(".az") else 0)
                scored[img_url] = boost * 10 + overlap
            if len(scored) >= 3:
                break
        if len(scored) >= 3:
            break
    ranked = sorted(scored, key=lambda u: -scored[u])
    checked = 0
    for img_url in ranked:
        if checked >= 3:
            break
        if not verify_image_url(img_url, min_dim=300):
            continue
        checked += 1
        ok_ = vision_confirms_product(client, img_url, product_name)
        if ok_ is None:   # AI yoxlaması mümkün olmadı: yalnız güclü uyğunluq (market/.az sayt + ad uyğunluğu) qəbul edilir
            ok_ = scored.get(img_url, 0) >= 12
        if ok_:
            image_cache[key] = img_url
            return img_url
    if today and _verify_used < VERIFY_MAX:
        image_cache[key] = f"none:{today}"
    return ""


def logo_ok(url: str, min_side: int = 48) -> bool:
    """Loqo kvadrata yaxın, kifayət qədər böyük və açıla bilən şəkil olmalıdır (banner/og:image deyil)."""
    if not url or not url.startswith("https://") or url.lower().split("?")[0].endswith(".svg"):
        return False
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200 or not r.content or len(r.content) > 1_500_000:
            return False
        im = Image.open(io.BytesIO(r.content))
        w, h = im.size
        return w >= min_side and h >= min_side and 0.7 <= w / float(h) <= 1.4
    except Exception:
        return False


def logo_candidates(domain: str) -> list[str]:
    base = f"https://{domain}"
    html = fetch_html(base, timeout=10)
    ranked = []
    if html:
        soup = BeautifulSoup(html, "html.parser")

        def size_of(tag):
            m = re.search(r"(\d+)x\d+", tag.get("sizes") or "")
            return int(m.group(1)) if m else 0

        for tag in soup.select('link[rel~="apple-touch-icon"], link[rel~="apple-touch-icon-precomposed"]'):
            if tag.get("href"):
                ranked.append((2, size_of(tag), urljoin(base, tag["href"])))
        for tag in soup.select('link[rel~="icon"], link[rel="shortcut icon"]'):
            if tag.get("href"):
                ranked.append((0, size_of(tag), urljoin(base, tag["href"])))
        man = soup.select_one('link[rel="manifest"]')
        if man and man.get("href"):
            try:
                data = SESSION.get(urljoin(base, man["href"]), timeout=8).json()
                for ic in data.get("icons", []):
                    sz = re.search(r"(\d+)x\d+", ic.get("sizes") or "")
                    ranked.append((1, int(sz.group(1)) if sz else 0, urljoin(urljoin(base, man["href"]), ic.get("src", ""))))
            except Exception:
                pass
    ranked.sort(key=lambda x: (-x[0], -x[1]))
    urls = [u for _, _, u in ranked]
    urls += [f"{base}/apple-touch-icon.png", f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
             f"https://icons.duckduckgo.com/ip3/{domain}.ico"]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


TG_AVATAR_SLUGS = {"Bravo": "bravosupermarketaze", "Araz": "arazsupermarket", "OBA": "obamarketlersebekesi", "AL Market": "almarketler",
                   "Rahat": "rahatsupermarket", "Bazarstore": "bazarstore_az"}
BANK_SITES = {"Birbank": "birbank.az", "Kapital Bank": "kapitalbank.az", "ABB": "abb-bank.az", "Leobank": "leobank.az",
              "Yelo Bank": "yelo.az", "Rabitəbank": "rabitabank.com", "Unibank": "unibank.az", "PAŞA Bank": "pashabank.az",
              "Xalq Bank": "xalqbank.az"}


def image_alive(url: str) -> bool:
    try:
        r = SESSION.get(url, timeout=10, stream=True)
        ok = r.status_code == 200 and "image" in (r.headers.get("Content-Type") or "image")
        r.close()
        return ok
    except Exception:
        return False


def _squash(t: str) -> str:
    t = (t or "").lower()
    for a_, b_ in (("ə", "e"), ("ş", "s"), ("ı", "i"), ("ç", "c"), ("ö", "o"), ("ü", "u"), ("ğ", "g")):
        t = t.replace(a_, b_)
    return re.sub(r"[^a-z0-9]", "", t)


def brand_text_matches(name: str, seen: str) -> bool:
    n, t = _squash(name), _squash(seen)
    if not t:
        return False
    variants = {n, n.replace("pasa", "pasha")}
    toks = [_squash(x) for x in name.split() if len(_squash(x)) >= 3 and _squash(x) not in ("bank", "market", "supermarket")]
    variants |= {x for x in toks} | {x.replace("pasa", "pasha") for x in toks}
    return any(v and v in t for v in variants)


def strict_verify(client, content: bytes, mime: str, name: str, kind: str, extra: str = "", trusted: bool = False) -> bool:
    """Gemini şəkildəki yazını oxuyur; brend/bank adı uyğun gəlməsə qəbul edilmir."""
    if kind == "logo":
        q = ('Bu şəkilə bax və YALNIZ JSON qaytar: {"isSingleLogo": true/false, "readText": "loqodakı oxunan yazı", "brand": "loqonun aid olduğu brend"}. '
             "isSingleLogo yalnız şəkil tək bir şirkət loqosudursa true olsun (məhsul, foto, ekran görüntüsü, banner, yazısız ümumi ikon, başqa mövzu = false).")
    else:
        q = ('Bu şəkilə bax və YALNIZ JSON qaytar: {"isPaymentCard": true/false, "bankOnCard": "kartda yazılan bank/loqo adı", "hasPeople": true/false, "isBannerOrNews": true/false}. '
             "isPaymentCard yalnız şəkildə təmiz plastik bank kartı əsas obyektdirsə true olsun.")
    data = extract_json_object(gemini_text(client, [q, types.Part.from_bytes(data=content, mime_type=mime)]))
    STATS["brand_verified"] += 1
    if kind == "logo":
        if data.get("isSingleLogo") is not True:
            return False
        seen = f"{data.get('readText') or ''} {data.get('brand') or ''}"
        return brand_text_matches(name, seen) or (trusted and not _squash(data.get("readText") or ""))
    if data.get("isPaymentCard") is not True or data.get("hasPeople") is True or data.get("isBannerOrNews") is True:
        return False
    return brand_text_matches(name, data.get("bankOnCard") or "")


def gemini_confirms_brand(client, url: str, name: str, kind: str, extra: str = "", trusted: bool = False) -> bool:
    try:
        r = SESSION.get(url, timeout=10)
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if r.status_code != 200 or len(r.content) > 3_000_000 or mime not in ("image/jpeg", "image/png", "image/webp"):
            return False
        try:
            from PIL import Image
            import io
            w, h = Image.open(io.BytesIO(r.content)).size
            if (kind == "card" and (w < 300 or h < 170)) or (kind == "logo" and (w < 96 or h < 96)):
                log.info("  [Brend yoxlama] %s/%s çox kiçik (%dx%d)", name, kind, w, h)
                return False
        except Exception:
            pass
        ok = strict_verify(client, r.content, mime, name, kind, extra, trusted)
        log.info("  [Brend yoxlama] %s/%s → %s %s", name, kind, "OK" if ok else "uyğun deyil", url[:80])
        return ok
    except Exception:
        return False


def facebook_logo_candidates(name: str) -> list[str]:
    """Rəsmi Facebook səhifəsinin profil şəkli (adətən rəsmi loqo)."""
    fb = FB_EXTRA.get(name) or (DEFAULT_SOCIAL_URLS.get(name) or {}).get("fb") or ""
    m = re.search(r"facebook\.com/([\w.\-]+)", fb)
    if not m:
        return []
    slug = m.group(1)
    out = []
    for u in (f"https://graph.facebook.com/{slug}/picture?width=400&height=400&redirect=true",
              f"https://graph.facebook.com/{slug}/picture?type=large"):
        try:
            r = SESSION.get(u, timeout=10, allow_redirects=True)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image") and r.url not in out:
                out.append(r.url)
                break
        except Exception:
            continue
    return out


FB_EXTRA = dict(VERIFIED_FB)


def social_logo_candidates(name: str) -> list[str]:
    """Telegram kanalının profil şəkli (adətən rəsmi loqo) — t.me səhifəsinin og:image-i."""
    slug = TG_AVATAR_SLUGS.get(name) or TELEGRAM_CHANNELS.get(name)
    if not slug:
        return []
    html = fetch_html(f"https://t.me/{slug}", timeout=12)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:image")
    return [og["content"]] if og and og.get("content", "").startswith("https://") else []


def bank_site_card_candidates(bank: str) -> list[str]:
    """Bankın öz saytındakı kart şəkilləri: 'card/kart' sözü olan, enli nisbətli şəkillər."""
    domain = BANK_SITES.get(bank)
    if not domain:
        return []
    pages = [f"https://{domain}/"] + [u for b, u, _ in CASHBACK_SOURCES if b == bank]
    out = []
    for page in pages[:2]:
        html = fetch_html(page, timeout=12)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        base = (soup.find("base") or {}).get("href") or page
        for im in soup.find_all("img"):
            src = im.get("data-src") or im.get("src") or ""
            hay = (src + " " + (im.get("alt") or "")).lower()
            if src and not src.startswith("data:") and re.search(r"card|kart|visa|mastercard", hay) and not re.search(r"logo|icon|\.svg", hay):
                u = urljoin(base, src)
                if u.startswith("https://") and u not in out:
                    out.append(u)
        og = soup.find("meta", property="og:image")
        if og and og.get("content", "").startswith("https://") and og["content"] not in out:
            out.append(og["content"])
    return out[:6]


def find_brand_image(client, name: str, kind: str) -> str:
    """Bütün mənbələrdə axtar: rəsmi sayt → sosial şəbəkə/Telegram → bütün internet (Bing); hamısı Gemini ilə təsdiqlənir."""
    cands = bank_site_card_candidates(name) if kind == "card" else (facebook_logo_candidates(name) + social_logo_candidates(name))
    for u in cands:
        if gemini_confirms_brand(client, u, name, kind, trusted=True):
            return u
    return search_verified_brand_image(client, name, kind)


_FOLD = str.maketrans({"ə": "e", "ı": "i", "ö": "o", "ü": "u", "ş": "s", "ç": "c", "ğ": "g", "İ": "i", "Ə": "e"})
_CARD_GENERIC = {"kart", "karti", "karta", "card", "cards", "bank", "banki", "cashback", "kesbek", "visa", "mastercard", "master",
                 "debit", "kredit", "credit", "plastik", "digital", "the", "and", "ile"}


def _fold(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").translate(_FOLD).lower()).strip()


def card_name_tokens(bank: str, card_name: str) -> list[str]:
    bank_words = set(_fold(bank).split())
    return [w for w in _fold(card_name).split() if len(w) >= 3 and w not in _CARD_GENERIC and w not in bank_words]


def official_card_image(client, bank: str, card_name: str, source_url: str = "") -> str:
    """Kartın şəklini YALNIZ bankın rəsmi saytından axtarır: kartın adı şəklin alt/yazısı/ətrafındakı mətndə keçməlidir,
    sonra Gemini şəklin təmiz bank kartı və bankın özünə aid olduğunu təsdiqləyir."""
    domain = BANK_SITES.get(bank)
    tokens = card_name_tokens(bank, card_name)
    if not domain or not tokens or client is None:
        return ""
    pages = []
    if source_url and domain in source_url:
        pages.append(source_url)
    for u in (f"https://{domain}/", f"https://www.{domain}/"):
        if u not in pages:
            pages.append(u)
    visited, cands = set(), []   # cands: (score, url)
    queue = pages[:3]
    follow_budget = 3
    while queue and len(visited) < 7:
        page = queue.pop(0)
        if page in visited:
            continue
        visited.add(page)
        html = fetch_html(page, timeout=12)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        base = (soup.find("base") or {}).get("href") or page
        for im in soup.find_all("img"):
            src = im.get("data-src") or im.get("data-lazy-src") or im.get("src") or ""
            if not src and im.get("srcset"):
                src = im["srcset"].split(",")[0].strip().split(" ")[0]
            if not src or src.startswith("data:"):
                continue
            low = src.lower()
            if re.search(r"logo|icon|favicon|sprite|flag|avatar|banner|\.svg|\.gif|qr|app-?store|google-?play", low):
                continue
            ctx = " ".join([src, im.get("alt") or "", im.get("title") or ""])
            node = im
            for _ in range(3):   # ən yaxın başlıq/mətn bloku
                node = node.parent
                if node is None:
                    break
                ctx += " " + node.get_text(" ", strip=True)[:160]
            folded = _fold(ctx)
            score = sum(1 for t in tokens if t in folded)
            if score >= len(tokens):
                cands.append((score + (1 if re.search(r"card|kart", low) else 0), urljoin(base, src)))
        if follow_budget > 0:   # kartın öz səhifəsinə keçid
            for a in soup.find_all("a", href=True):
                if follow_budget <= 0:
                    break
                txt = _fold(a.get_text(" ", strip=True) + " " + a["href"])
                if all(t in txt for t in tokens):
                    href = urljoin(base, a["href"])
                    if domain in href and href not in visited and href not in queue:
                        queue.append(href)
                        follow_budget -= 1
    seen_u, ranked = set(), []
    for sc, u in sorted(cands, key=lambda x: -x[0]):
        if u.startswith("https://") and u not in seen_u:
            seen_u.add(u)
            ranked.append(u)
    for url in ranked[:4]:
        try:
            r = SESSION.get(url, timeout=12)
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status_code != 200 or len(r.content) > 3_000_000 or len(r.content) < 4000 or mime not in ("image/jpeg", "image/png", "image/webp"):
                continue
            w, h = Image.open(io.BytesIO(r.content)).size
            if w < 260 or h < 150 or not (1.25 <= w / float(h) <= 2.1):
                continue
            ok = strict_verify(client, r.content, mime, bank, "card", card_name)
            log.info("  [Rəsmi kart şəkli] %s — %s: %s %s", bank, card_name, "OK" if ok else "uyğun deyil", url[:90])
            if ok:
                return url
        except Exception as exc:
            log.info("  [Rəsmi kart şəkli] %s xəta: %s", bank, str(exc)[:100])
    return ""


def search_verified_brand_image(client, name: str, kind: str, extra: str = "") -> str:
    """İnternetdə (Bing) loqo / bank kartı şəkli axtarır; yalnız Gemini təsdiqlədikdə qəbul edir."""
    if DDGS is None or client is None or types is None:
        return ""
    is_market = name in [m for m, _, _ in MARKET_WEBSITES]
    if kind == "logo":
        queries = [f"{name} {'supermarket ' if is_market else 'bank '}Azerbaijan logo", f"{name} loqo az"]
    else:
        queries = ([f"{name} {extra} kartı", f"{name} {extra} card"] if extra else
                   [f"{name} Azərbaycan bank kartı", f"{name} bank card Azerbaijan visa mastercard"])
    results = []
    for query in queries:
        try:
            time.sleep(0.5)
            results += DDGS().images(query, max_results=12, backend="bing") or []
        except Exception as exc:
            log.debug("  [Brend şəkil axtarışı] %s: %s", name, type(exc).__name__)
    base = re.sub(r"[^a-z0-9ə]", "", name.lower().split()[0].replace("ş", "s").replace("ı", "i"))
    seen_u = set()
    cands = []
    for it in results:
        url = it.get("image") or ""
        w, h = int(it.get("width") or 0), int(it.get("height") or 0)
        if url in seen_u:
            continue
        seen_u.add(url)
        if re.search(r"ytimg|youtube|pinterest|pinimg|behance|imgflip|alamy|shutterstock", url):
            continue
        if not url.startswith("https://") or BAD_IMG_RE.search(url.replace(".svg", "")) and kind == "card":
            continue
        if w and h and (w < 120 or h < 120):
            continue
        ratio = (w / float(h)) if w and h else 1.0
        if kind == "logo" and not (0.6 <= ratio <= 1.7):
            continue
        if kind == "card" and not (1.2 <= ratio <= 2.0):
            continue
        hay = (url + " " + (it.get("title") or "")).lower()
        rel = 1 if any(b_ in hay for b_ in {base, base.replace("pasa", "pasha"), base.replace("pasa", "paşa")}) else 0
        if rel:
            cands.append((rel, w * h, url))
    cands.sort(reverse=True)
    log.info("  [Brend axtarış] %s/%s: %d nəticə, %d namizəd", name, kind, len(results), len(cands))
    for _, _, url in cands[:4]:
        try:
            r = SESSION.get(url, timeout=10)
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status_code != 200 or len(r.content) > 3_000_000 or mime not in ("image/jpeg", "image/png", "image/webp"):
                continue
            ok = strict_verify(client, r.content, mime, name, kind, extra)
            log.info("  [Brend yoxlama] %s %s → %s %s", name, extra, "OK" if ok else "uyğun deyil", url[:90])
            if ok:
                return url
        except Exception as exc:
            log.info("  [Brend yoxlama] %s xəta: %s", name, str(exc)[:100])
            continue
    return ""


def collect_brand_logos(existing_logos: dict, client=None) -> dict:
    logos = dict(existing_logos or {})
    brands = [(m, dom) for m, _, dom in MARKET_WEBSITES]
    brands += [(b, dom) for b, _, dom in CASHBACK_SOURCES if b != "FED.az"]
    brands += list(SEARCH_BANKS)
    todo = [(n, d) for n, d in brands if not (logos.get(n) or "").startswith("https://")]

    def find(item):
        name, domain = item
        cands = logo_candidates(domain)
        for cand in cands[:8]:
            if logo_ok(cand) and (client is None or gemini_confirms_brand(client, cand, name, "logo", trusted=True)):
                return name, cand
        for cand in cands:  # son çarə: favicon xidmətləri (kiçik ölçü qəbul edilir)
            if ("favicons" in cand or "duckduckgo" in cand) and logo_ok(cand, min_side=32) and (client is None or gemini_confirms_brand(client, cand, name, "logo", trusted=True)):
                return name, cand
        return name, ""

    market_names = {m for m, _, _ in MARKET_WEBSITES}

    def find_with_search(item):
        if item[0] in market_names:   # marketlər üçün əvvəl rəsmi Facebook profil şəkli
            for u in facebook_logo_candidates(item[0]):
                if gemini_confirms_brand(client, u, item[0], "logo", trusted=True):
                    return item[0], u
        n, u = find(item)
        return (n, u) if u else (n, find_brand_image(client, item[0], "logo"))
    find_fn = find_with_search if client is not None else find
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, url in pool.map(find_fn, todo):
            if url:
                logos[name] = url
            else:
                logos.pop(name, None)   # yanlış loqo göstərməkdənsə, saytda baş hərflər göstərilir
                log.warning("  [Loqo] %s üçün uyğun loqo tapılmadı", name)
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


def page_to_block(soup, market: str, page_url: str, base: str = "") -> dict | None:
    base = base or page_url
    for tag in soup(["script", "style", "noscript", "svg", "footer", "nav"]):
        tag.decompose()
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src or src.startswith("data:"):
            continue
        full = urljoin(base, src)
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
    out = {"market": market, "blocks": [], "structured": [], "socials": {}, "telegram": "", "vision": []}
    html = fetch_html(url)
    if not html:
        return out
    soup0 = BeautifulSoup(html, "html.parser")
    base0 = (soup0.find("base") or {}).get("href") or url
    pages = [(url, soup0)]
    for extra in discover_promo_links(soup0, base0)[:EXTRA_PAGES]:
        h = fetch_html(extra, timeout=14)
        if h:
            pages.append((extra, BeautifulSoup(h, "html.parser")))

    tg = {}
    for page_url, soup in pages:
        base = (soup.find("base") or {}).get("href") or page_url
        promo_page = any(k in page_url.lower() for k in PROMO_KEYWORDS)
        discover_socials(soup, out["socials"], tg)
        out["structured"] += extract_structured_products(soup, market, page_url, promo_page)
        out["structured"] += extract_cards_generic(soup, market, base, promo_page)
        if market not in ("Bazarstore", "Araz") and (promo_page or page_url == url):
            out["vision"] += collect_promo_images(soup, base, market, VISION_PER_MARKET)
        block = page_to_block(soup, market, page_url, base)  # soup-u dəyişir, sonda çağırılır
        if block:
            out["blocks"].append(block)
        STATS["web_pages"] += 1
    out["vision"] = out["vision"][:VISION_PER_MARKET]
    out["telegram"] = tg.get("slug", "")
    log.info("  [Veb] %s: %d səhifə, %d məhsul kartı, %d promo şəkil, ig=%s fb=%s tg=%s", market, len(pages), len(out["structured"]),
             len(out["vision"]), bool(out["socials"].get("ig")), bool(out["socials"].get("fb")), out["telegram"] or "-")
    return out


def scrape_market_websites() -> tuple[list[dict], list[dict], dict, dict, list[dict]]:
    socials = {k: dict(v) for k, v in DEFAULT_SOCIAL_URLS.items()}
    telegram = dict(TELEGRAM_CHANNELS)
    blocks, structured, vision = [], [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda m: scrape_one_market(m[0], m[1]), MARKET_WEBSITES))
    for res in results:
        blocks += res["blocks"]
        structured += res["structured"]
        vision += res["vision"]
        if res["socials"]:
            socials.setdefault(res["market"], {}).update(res["socials"])  # saytdan tapılan rəsmi ünvan üstünlük qazanır
        if res["telegram"]:
            telegram[res["market"]] = res["telegram"]
    for m_, fb_ in VERIFIED_FB.items():
        socials.setdefault(m_, {})["fb"] = fb_
    for pair in (os.environ.get("TELEGRAM_EXTRA") or "").split(","):
        if ":" in pair:
            m, slug = pair.split(":", 1)
            telegram[m.strip()] = slug.strip().lstrip("@")
    STATS["structured"] = len(structured)
    return blocks, structured, socials, telegram, vision


# --------------------------------------------------------------------------- saytlara xas çıxarıcılar
CATEGORY_RULES = [   # sıra vacibdir: dəqiq kateqoriyalar əvvəldə; açar sözlər sadələşdirilmiş (ə→e, ı→i, ö→o, ü→u, ş→s, ç→c, ğ→g) yazılır
    ("Heyvan yemi", ("it yemi", "pisik yemi", "pisik qidasi", "it qidasi", "whiskas", "pedigree", "kitekat", "felix", "purina", "heyvan yem", "quş yemi", "balig yemi")),
    ("Uşaq məhsulları", ("usaq", "bebek", "pampers", "huggies", "bebe", "mamasi", "mama ", "uşaq", "carriage", "nan ", "nestogen", "aptamil", "hipp", "memoris", "bez ")),
    ("Dondurulmuş məhsullar", ("dondurulmus", "donmus", "frozen", "pelmen", "xinkali", "naget", "nugget", "dondurma", "pizza", "dolma donm")),
    ("Süd məhsulları", ("sud", "qatiq", "pendir", "kere yag", "kefir", "qaymaq", "yoqurt", "yogurt", "ayran", "kesmik", "danone", "activia", "xama", "yumurta", "maqnezyum")),
    ("Ət və quş əti", ("et ", "toyuq", "qiyme", "kolbasa", "sosis", "sucuq", "vetchina", "vetcina", "dana ", "quzu", "bud ", "kabab", "ciger", "delikates", "salami", "bacon", "bekon", "ordek", "hindusk", "brisket")),
    ("Balıq və dəniz məhsulları", ("baliq", "qizilbaliq", "som ", "kilka", "skumbriya", "krevet", "ikra", "tuna", "sardina", "karides", "midye", "kalmar", "kutum", "siyenek")),
    ("Meyvə və tərəvəz", ("alma", "armud", "banan", "portagal", "mandarin", "limon", "uzum", "xiyar", "pomidor", "kartof", "soyan", "sogan", "yerkoku", "kelem", "bibər", "biber", "badimcan", "kivi", "nar ", "heyva", "seftali", "ərik", "erik", "gilas", "albali", "gavali", "qarpiz", "yemis", "meyve", "terevez", "göyərti", "goyerti", "sarimsaq", "mantar", "avokado", "ananas", "xurma", "findiq alma", "limon")),
    ("Çörək və un məmulatları", ("corek", "lavas", "bulka", "kruasan", "baget", "tandir", "un ", "xemir", "mayasi", "sehriyye", "pirojki", "keks", "sirnik", "yuxa", "tost corek", "lahmacun")),
    ("Bakaliya", ("duyu", "yarma", "bugda", "mercimek", "lobya", "noxud", "makaron", "spagetti", "seker", "duz ", "yag ", "gunebaxan", "zeytun yag", "yagi", "sirke", "ketcup", "ketchup", "mayonez", "sous", "sous ", "konserv", "bal ", "mürebbe", "murebbe", "pasta", "bulyon", "ədviyyat", "edviyyat", "kakao", "pudra", "vanil", "mayasi", "hazir sorba", "noodle", "doshirak", "fasulye", "qargidali")),
    ("İçkilər", ("kola", "cola", "su ", "sirə", "sire", "icki", "fanta", "limonad", "sprite", "pepsi", "cay", "qehve", "kofe", "nescafe", "coffee", "enerji", "mineral", "gazli", "kompot", "kvas", "ice tea", "nektar", "sok ", "juice", "olimpik", "badamli", "pivə", "pive", "sarab", "araq", "vodka", "viski", "konyak", "shampan", "sampan", "şərab")),
    ("Şirniyyat", ("sokolad", "konfet", "pecenye", "vafli", "tort", "biskvit", "marmelad", "halva", "paxlava", "snickers", "twix", "mars ", "bounty", "kinder", "milka", "alpen", "jele", "zefir", "draje", "naxis", "lokum", "saqqiz", "bar ", "sirniyyat", "sekerleme", "kruvasan sokolad", "nutella", "krem pasta")),
    ("Qəlyanaltı və qoz-fındıq", ("cips", "chips", "kraker", "sneck", "snack", "findiq", "badam", "qoz ", "pistaci", "fistiq", "kurag", "qurudulmus", "popkorn", "pop corn", "pretzel", "lays", "pringles", "doritos", "cheetos", "susam", "gunebaxan toxum", "toxum")),
    ("Təmizlik və məişət kimyası", ("yuyucu", "toz ", "gel ", "bulasiq", "temizl", "agardici", "ariel", "persil", "tide", "fairy", "domestos", "comet", "vanish", "lenor", "kondisioner", "yuma", "dezinfeks", "ətir", "hava tazələ", "hava tazele", "rahatlandirici", "mayesi", "maye ", "pakyet", "zibil", "salfet", "tualet kagiz", "kagiz dəsmal", "kagiz desmal", "sunger", "əlcək", "elcek")),
    ("Şəxsi gigiyena və gözəllik", ("sampun", "shampoo", "sabun", "dis pastasi", "dis fircasi", "diş", "deodorant", "krem", "losyon", "ustara", "tiraş", "tiras", "gigiyenik", "prokladka", "always", "libresse", "tampon", "pomada", "boya", "lak ", "parfum", "odekolon", "dus geli", "maska", "serum", "nivea", "dove", "head shoulders", "pantene", "colgate", "oral-b", "gillette", "pambiq", "qulaq cubugu", "islak desmal", "salfet islak")),
    ("Ev və mətbəx", ("qab ", "qazan", "tava", "bicaq", "qasiq", "stakan", "fincan", "termos", "qapaq", "ev ucun", "mətbəx", "metbex", "tekstil", "yorgan", "balis", "carsaf", "xalca", "lampa", "batareya", "ampul", "uzadici", "kabel", "alətlər", "aletler", "oyuncaq", "kitab", "dəftər", "defter", "qələm", "qelem", "paltar", "corab", "ayaqqabi", "terlik", "geyim", "elektrik", "çaydan", "caydan", "ütü", "utu", "blender", "mikser", "bisiricisi", "tozsoran", "televizor", "qizdirici", "fen ", "tost aparati", "mebel", "plastik", "yeşik", "yesik")),
]
UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?\s?(?:kq|kg|qr|gr|ml|litr|lt|ədəd|əd)\b|\d+(?:[.,]\d+)?\s?[lLqQgG]\b|\bKQ\b)", re.I)
BAD_IMG_RE = re.compile(r"logo|icon|favicon|sprite|flag|avatar|arrow|header|footer|wolt|facebook\.com/tr|yandex|linkedin|"
                        r"placeholder|noimage|no-image|banner-app|app-download|\.svg|\.gif", re.I)


def tr_title(text: str) -> str:
    """BÜTÖV BÖYÜK HƏRFLƏ yazılmış adı oxunaqlı hala salır (Azərbaycan/Türk i-İ qaydası ilə)."""
    if not text or not text.isupper():
        return text
    low = text.replace("İ", "i").replace("I", "ı").lower()
    words = []
    for w in low.split(" "):
        if w in ("kq", "ml", "qr", "gr", "st", "l"):
            words.append(w.upper() if w in ("kq", "ml", "qr", "gr", "st") else w.upper())
        elif w:
            first = "İ" if w[0] == "i" else ("I" if w[0] == "ı" else w[0].upper())
            words.append(first + w[1:])
    return " ".join(words)


_FOLD_MAP = str.maketrans({"ə": "e", "ı": "i", "ö": "o", "ü": "u", "ş": "s", "ç": "c", "ğ": "g", "Ə": "e", "İ": "i", "I": "i", "Ö": "o", "Ü": "u", "Ş": "s", "Ç": "c", "Ğ": "g"})


def _cat_fold(text: str) -> str:
    return " " + re.sub(r"\s+", " ", (text or "").translate(_FOLD_MAP).lower()) + " "


def guess_category(name: str) -> str:
    low = _cat_fold(name)
    for cat, kws in CATEGORY_RULES:
        for k in kws:
            kf = k.translate(_FOLD_MAP).lower()
            if (kf.endswith(" ") and (" " + kf) in low) or (not kf.endswith(" ") and kf in low):
                return cat
    return "Digər"


def guess_unit(name: str) -> str:
    m = UNIT_RE.search(name or "")
    if not m:
        return "1 ədəd"
    u = m.group(1).strip()
    return "1 kq" if u.lower() == "kq" else u


def price_in(text: str):
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", text or "")
    return safe_float(m.group(1)) if m else None


def img_of(el, base: str) -> str:
    """Elementdəki real məhsul şəklini tapır (lazy-load daxil), loqo/boş şəkilləri atır."""
    for im in el.find_all("img"):
        for attr in ("data-lazyloadsrc", "data-src", "data-lazy-src", "data-original", "src"):
            src = im.get(attr) or ""
            if src and not src.startswith("data:") and not BAD_IMG_RE.search(src):
                url = urljoin(base, src)
                if url.startswith("https://"):
                    return url
    return ""


def parse_bazarstore_page(html: str) -> list[dict]:
    items = []
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select(".product-item"):
        title = card.select_one(".product-title")
        name = title.get_text(" ", strip=True) if title else ""
        actual = card.select_one(".actual-price")
        old = card.select_one(".old-price")
        price = price_in(actual.get_text() if actual else (card.select_one(".price").get_text() if card.select_one(".price") else ""))
        if not name or not price:
            continue
        items.append({
            "name": tr_title(name), "price": price,
            "oldPrice": price_in(old.get_text()) if old else None, "market": "Bazarstore", "source": "Veb sayt",
            "imageUrl": img_of(card, "https://bazarstore.az/"), "category": guess_category(name), "unit": guess_unit(name),
        })
    return items


def scrape_bazarstore() -> list[dict]:
    """Bazarstore endirimli məhsullar səhifəsi (hər səhifədə köhnə + yeni qiymət və şəkil var)."""
    base = "https://bazarstore.az/endirimli-m%C9%99hsullar"
    first = parse_bazarstore_page(fetch_html(base + "?pagesize=60", timeout=25))
    size_param = "&pagesize=60" if len(first) > 6 else ""
    if not size_param:
        first = parse_bazarstore_page(fetch_html(base, timeout=25))
    if not first:
        return []
    per_page = len(first)
    pages = min(-(-BAZAR_MAX // per_page), 45)

    def one(n):
        return parse_bazarstore_page(fetch_html(f"{base}?pagenumber={n}{size_param}", timeout=25))

    items = list(first)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for chunk in pool.map(one, range(2, pages + 1)):
            items += chunk
    uniq = {}
    for it in items:
        uniq.setdefault(normalize_key(it["name"]), it)
    out = list(uniq.values())[:BAZAR_MAX]
    STATS["bazar"] = len(out)
    log.info("  [Bazarstore] %d endirimli məhsul (səhifə başına %d, %d səhifə)", len(out), per_page, pages)
    return out


def scrape_araz() -> list[dict]:
    """Araz-ın öz API-si (sayt bu məlumatı JS ilə yükləyir): endirimli və 'wow' kampaniya məhsulları."""
    host = "b7x9kq.arazmarket.az"
    html = fetch_html("https://www.arazmarket.az/", timeout=25)
    m = re.search(r"https://([\w-]+\.arazmarket\.az)/storage", html or "")
    if m:
        host = m.group(1)
    try:
        data = SESSION.get(f"https://{host}/api/home", timeout=25).json().get("data", {})
    except Exception as exc:
        log.warning("  [Araz API] %s", type(exc).__name__)
        return []
    out = []
    for key in ("discount_products", "wow_campaign_products", "products_of_the_day"):
        for p in data.get(key) or []:
            if not isinstance(p, dict) or not p.get("is_discount"):
                continue
            price, old = safe_float(p.get("discount_price")), safe_float(p.get("sales_price"))
            imgs = p.get("images") or []
            first = imgs[0] if imgs else ""
            img = first.get("url") if isinstance(first, dict) else first
            if not price or not p.get("title"):
                continue
            out.append({
                "name": p["title"], "price": price, "oldPrice": old, "market": "Araz", "source": "Veb sayt",
                "imageUrl": img if isinstance(img, str) and img.startswith("https://") else "",
                "category": guess_category(f"{p['title']} {p.get('category_title') or ''}"), "unit": guess_unit(p["title"]),
            })
    uniq = {}
    for it in out:
        uniq.setdefault(normalize_key(it["name"]), it)
    STATS["araz"] = len(uniq)
    log.info("  [Araz API] %d endirimli məhsul", len(uniq))
    return list(uniq.values())


CATALOG_BUDGET_SEC = env_int("CATALOG_BUDGET_SEC", 900)
CATALOG_MAX_ITEMS = env_int("CATALOG_MAX_ITEMS", 6000)


def bazar_category_urls() -> list[str]:
    html = fetch_html("https://bazarstore.az/", timeout=25)
    soup = BeautifulSoup(html, "html.parser")
    hrefs = []
    for a in soup.select("a[href]"):
        h = urljoin("https://bazarstore.az/", a["href"]).split("#")[0]
        if h not in hrefs:
            hrefs.append(h)
    try:
        i0 = hrefs.index("https://bazarstore.az/cart")
    except ValueError:
        return []
    end = next((i for i, h in enumerate(hrefs) if "endirimli" in unquote(h).lower()), len(hrefs))
    return [h for h in hrefs[i0 + 1:end] if h.startswith("https://bazarstore.az/") and h.count("/") == 3]


def scrape_bazarstore_catalog(deadline: float) -> list[dict]:
    """Bazarstore-un bütün kateqoriyalarını gəzib adi + endirimli cari qiymətləri yığır (müqayisə üçün)."""
    cats = bazar_category_urls()
    log.info("  [Bazar kataloq] %d kateqoriya tapıldı", len(cats))
    seen: dict[str, dict] = {}

    def crawl(url):
        for n in range(1, 9):
            if time.time() > deadline or len(seen) >= CATALOG_MAX_ITEMS:
                return
            items = parse_bazarstore_page(fetch_html(f"{url}?pagesize=60&pagenumber={n}", timeout=25))
            if not items:
                return
            new = 0
            for it in items:
                k = normalize_key(it["name"])
                if k and k not in seen:
                    seen[k] = {"n": clean_text(it["name"], 70), "p": it["price"], "o": it.get("oldPrice") or 0}
                    new += 1
            if new == 0 or len(items) < 10:
                return

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(crawl, cats))
    log.info("  [Bazar kataloq] %d məhsul yığıldı", len(seen))
    return list(seen.values())


def _araz_products(obj, out: dict):
    if isinstance(obj, dict):
        if obj.get("title") and (obj.get("sales_price") or obj.get("discount_price")) and obj.get("id") is not None:
            out[obj["id"]] = obj
            return
        for v in obj.values():
            _araz_products(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _araz_products(v, out)


def scrape_araz_catalog(deadline: float) -> list[dict]:
    """Araz API-sindən kateqoriya üzrə bütün məhsulları çəkməyə çalışır (API ünvanları sınaqla müəyyən edilir)."""
    host = "b7x9kq.arazmarket.az"
    m = re.search(r"https://([\w-]+\.arazmarket\.az)/storage", fetch_html("https://www.arazmarket.az/", timeout=25) or "")
    if m:
        host = m.group(1)
    base = f"https://{host}/api"
    found: dict = {}
    cats = []
    for path in ("/categories", "/products?page=1", "/products", "/category-tree", "/menu"):
        try:
            r = SESSION.get(base + path, timeout=20)
            log.info("  [Araz kataloq] %s → HTTP %s (%d bayt)", path, r.status_code, len(r.content))
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        _araz_products(data, found)

        def walk_cats(o):
            if isinstance(o, dict):
                if o.get("id") is not None and (o.get("slug") or o.get("title")) and not (o.get("sales_price") or o.get("discount_price")):
                    cats.append((o["id"], o.get("slug") or ""))
                for v in o.values():
                    walk_cats(v)
            elif isinstance(o, list):
                for v in o:
                    walk_cats(v)
        walk_cats(data.get("data", data) if isinstance(data, dict) else data)
    cats = list(dict.fromkeys(cats))[:500]
    log.info("  [Araz kataloq] %d kateqoriya, ilkin %d məhsul", len(cats), len(found))
    templates = ["/products?category_id={id}&page={n}", "/products?category={slug}&page={n}", "/category/{slug}?page={n}",
                 "/categories/{id}/products?page={n}"]
    tpl = None
    for cid, slug in cats[:6]:
        for t in templates:
            try:
                r = SESSION.get(base + t.format(id=cid, slug=slug, n=1), timeout=20)
                before = len(found)
                if r.status_code == 200:
                    _araz_products(r.json(), found)
                if len(found) > before:
                    tpl = t
                    break
            except Exception:
                continue
        if tpl:
            break
    if tpl:
        log.info("  [Araz kataloq] işləyən ünvan: %s", tpl)

        def crawl(cat):
            cid, slug = cat
            for n in range(1, 11):
                if time.time() > deadline or len(found) >= CATALOG_MAX_ITEMS:
                    return
                try:
                    r = SESSION.get(base + tpl.format(id=cid, slug=slug, n=n), timeout=20)
                    before = len(found)
                    if r.status_code == 200:
                        _araz_products(r.json(), found)
                    if len(found) == before:
                        return
                except Exception:
                    return
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(crawl, cats))
    else:
        log.warning("  [Araz kataloq] kateqoriya API-si tapılmadı — yalnız ana səhifə məhsulları")
    out = []
    for p_ in found.values():
        price, old = safe_float(p_.get("discount_price")) or safe_float(p_.get("sales_price")), safe_float(p_.get("sales_price"))
        if price:
            out.append({"n": clean_text(p_["title"], 70), "p": price, "o": old if old and old > price else 0})
    log.info("  [Araz kataloq] %d məhsul yığıldı", len(out))
    return out


def scrape_full_catalogs() -> dict:
    deadline = time.time() + CATALOG_BUDGET_SEC
    res = {}
    for name, fn in (("Bazarstore", scrape_bazarstore_catalog), ("Araz", scrape_araz_catalog)):
        try:
            res[name] = fn(deadline)
        except Exception as exc:
            log.warning("  [%s kataloq xətası] %s", name, str(exc)[:160])
            res[name] = []
    return res


class CatalogIndex:
    """Marketin bütün kataloqunda ad + çəki üzrə eyni məhsulu tapır."""

    def __init__(self, market: str, items: list[dict]):
        self.market, self.entries, self.inv = market, [], {}
        for it in items:
            toks = core_tokens(it["n"])
            if not toks:
                continue
            idx = len(self.entries)
            self.entries.append({"name": it["n"], "price": it["p"], "old": it.get("o") or 0, "tok": toks})
            for t in toks:
                if len(t) >= 4:
                    self.inv.setdefault(t, []).append(idx)

    def find(self, name: str):
        toks = core_tokens(name)
        if not toks:
            return None
        cands = set()
        for t in sorted(toks, key=len, reverse=True)[:2]:
            cands.update(self.inv.get(t, [])[:400])
        probe = {"name": name, "unit": ""}
        best = None
        for i in cands:
            e = self.entries[i]
            if same_product(probe, {"name": e["name"], "unit": ""}) or \
               (len(toks & e["tok"]) >= 2 and size_sig(name) in (None, size_sig(e["name"]))):
                if best is None or e["price"] < best["price"]:
                    best = e
        return best


def extract_cards_generic(soup, market: str, base: str, promo_page: bool) -> list[dict]:
    """Ümumi məhsul kartı çıxarıcısı: başlıq + şəkil + ₼ qiymət olan ən kiçik blokları tapır."""
    if len(str(soup)) > 700_000:
        return []
    price_re = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:₼|AZN|azn)")
    cands = []
    for el in soup.find_all(["div", "li", "article", "a"]):
        txt = el.get_text(" ", strip=True)
        if not txt or len(txt) > 400 or not price_re.search(txt) or el.find("img") is None:
            continue
        cands.append(el)
    cand_ids = {id(c) for c in cands}
    out = []
    for el in cands:
        if any(id(d) in cand_ids for d in el.find_all(["div", "li", "article", "a"])):
            continue  # daha kiçik uyğun blok var
        head = el.find(re.compile(r"^h[1-6]$")) or el.select_one('[class*="title"], [class*="name"]')
        name = head.get_text(" ", strip=True) if head else (el.find("img").get("alt") or "").strip()
        if not name or len(name) < 3 or price_re.search(name):
            continue
        old_el = el.select_one('del, s, strike, [class*="old"], [class*="through"]')
        old = price_in(old_el.get_text()) if old_el else None
        prices = [safe_float(x.replace(",", ".")) for x in price_re.findall(el.get_text(" ", strip=True))]
        prices = [x for x in prices if x]
        if not prices:
            continue
        price = min(prices)
        if old is None and len(prices) >= 2 and max(prices) > price:
            old = max(prices)
        if old is None and not promo_page:
            continue  # endirim əlaməti yoxdur
        out.append({"name": name, "price": price, "oldPrice": old, "market": market, "source": "Veb sayt",
                    "imageUrl": img_of(el, base), "category": guess_category(name), "unit": guess_unit(name)})
    STATS["cards"] += len(out)
    return out[:80]


def collect_promo_images(soup, base: str, market: str, limit: int) -> list[dict]:
    """Aksiya/kataloq şəkilləri (qiymət şəkildə yazılır) — Vision AI ilə oxunacaq."""
    seen, out = set(), []
    for im in soup.find_all("img"):
        src = ""
        for attr in ("data-lazyloadsrc", "data-src", "data-lazy-src", "src"):
            v = im.get(attr) or ""
            if v and not v.startswith("data:"):
                src = v
                break
        if not src or BAD_IMG_RE.search(src):
            continue
        url = urljoin(base, src)
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            out.append({"market": market, "source": "Veb sayt", "imageUrl": url})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- Apify (Instagram, Facebook)
def apify_run(actor: str, token: str, payload: dict, label: str) -> list:
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

    def once():
        r = requests.post(endpoint, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=240)
        if r.status_code in (200, 201):
            data = r.json()
            return data if isinstance(data, list) else []
        try:
            _j = r.json().get("error", {})
            _msg = f"{_j.get('type','')} / {str(_j.get('message',''))[:150]}"
        except Exception:
            _msg = "cavab oxunmadı, uzunluq %d" % len(r.text)
        log.error("  [Apify %s xəta] HTTP %s | %s", label, r.status_code, _msg.replace(token, "<token>") if token else _msg)
        if r.status_code in (400, 401, 402, 403):   # təkrar cəhdin mənası yoxdur (kredit/icazə problemi)
            return None
        raise RuntimeError(f"HTTP {r.status_code}")

    res = with_retry(once, f"Apify {label}", tries=2, delay=20, is_empty=lambda r: r is None or r == [], default=[])
    return res or []


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
                      {"directUrls": ig_urls[:14], "resultsType": "posts", "resultsLimit": IG_POSTS}, "Instagram")
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
        if img:
            vision.append({"market": market, "source": "Instagram", "imageUrl": img, "caption": caption[:700]})
    return blocks, vision


def scrape_facebook_via_apify(token: str, socials: dict) -> tuple[list[dict], list[dict]]:
    fb_urls = [l["fb"] for l in socials.values() if l.get("fb") and "facebook.com/" in l["fb"]]
    if not fb_urls:
        return [], []
    log.info("  [Apify Facebook] %d səhifə (hərəsindən son %d paylaşım)...", len(fb_urls), FB_POSTS)
    posts = apify_run("automly~facebook-page-posts-scraper", token,
                      {"pages": fb_urls[:14], "maxPosts": FB_POSTS}, "Facebook")
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
        if img:
            vision.append({"market": page, "source": "Facebook", "imageUrl": img, "caption": text[:700]})
    return blocks, vision


# --------------------------------------------------------------------------- Telegram
def scrape_one_telegram(market: str, slug: str) -> tuple[list[dict], list[dict]]:
    """Kanalın son MAX_AGE_DAYS günlük bütün paylaşımlarını oxuyur (səhifə-səhifə, ?before= ilə geriyə)."""
    posts, vision = [], []
    seen_ids, before, n_pages, total = set(), None, 0, 0
    while n_pages < TG_PAGES:
        url = f"https://t.me/s/{slug}" + (f"?before={before}" if before else "")
        html = fetch_html(url, timeout=15)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        wraps = soup.select(".tgme_widget_message_wrap")
        if not wraps:
            break
        n_pages += 1
        ids, any_recent = [], False
        for wrap in wraps:
            msg = wrap.select_one(".tgme_widget_message")
            pid = 0
            if msg and msg.get("data-post"):
                try:
                    pid = int(str(msg["data-post"]).rsplit("/", 1)[-1])
                except ValueError:
                    pid = 0
            if pid:
                ids.append(pid)
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
            time_el = wrap.select_one("time")
            if not is_recent(time_el.get("datetime") if time_el else None):
                continue
            any_recent = True
            txt_el = wrap.select_one(".tgme_widget_message_text")
            text = txt_el.get_text(" ", strip=True) if txt_el else ""
            imgs = []
            for el in wrap.select(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb, "
                                  ".tgme_widget_message_roundvideo_thumb, .link_preview_image, .link_preview_right_image, "
                                  ".tgme_widget_message_link_preview .link_preview_image"):
                m = re.search(r"url\(['\"]?(https?://[^'\")]+)", el.get("style") or "")
                if m and m.group(1) not in imgs:
                    imgs.append(m.group(1))
            for el in wrap.select("img"):
                src = el.get("src") or ""
                if src.startswith("http") and src not in imgs and not any(x in src for x in ("emoji", "userpic", "avatar")):
                    imgs.append(src)
            img = imgs[0] if imgs else ""
            STATS["tg_posts"] += 1
            total += 1
            cap_text = txt_el.get_text("\n", strip=True) if txt_el else ""
            if len(text) > 15:
                posts.append({"text": text[:500], "imageUrl": img})
            for im in imgs[:6]:
                vision.append({"market": market, "source": "Telegram kanalı", "imageUrl": im, "caption": cap_text[:700]})
        if not any_recent or not ids or min(ids) <= 1:
            break
        nb = min(ids)
        if nb == before:
            break
        before = nb
    log.info("  [Kanal] %s (t.me/%s): %d səhifə, %d paylaşım, %d şəkilli", market, slug, n_pages, total, len(vision))
    blocks = [{"market": market, "source": "Telegram kanalı", "posts": posts}] if posts else []
    return blocks, vision


def scrape_telegram_channels(channels: dict) -> tuple[list[dict], list[dict]]:
    blocks, vision = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for b, v in pool.map(lambda kv: with_retry(lambda: scrape_one_telegram(*kv), f"Telegram {kv[0]}", tries=2, delay=5,
                                                   is_empty=lambda r: not r[0] and not r[1], default=([], [])), channels.items()):
            blocks += b
            vision += v
    return blocks, vision


# --------------------------------------------------------------------------- Vision AI
def download_image_part(url: str):
    try:
        r = SESSION.get(url, timeout=12)
        if r.status_code != 200 or len(r.content) > 4_500_000 or len(r.content) < 3000:
            return None
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            mime = "image/jpeg"
        return types.Part.from_bytes(data=r.content, mime_type=mime)
    except Exception:
        return None


def confirm_vision_items(client, items: list[dict]) -> list[dict]:
    """İkinci, müstəqil oxuma: şəkildəki yazı ilə çıxarılan ad və qiymət uyğun gəlmirsə məhsul atılır."""
    by_img: dict[str, list[dict]] = {}
    for it in items:
        by_img.setdefault(it.get("_img") or "", []).append(it)
    kept, dropped = [], 0

    def confirm(url, group):
        part = download_image_part(url) if url else None
        if part is None:
            return []
        listing = "\n".join(f"{i + 1}. ad: {g['name']} | qiymət: {g['price']} AZN" for i, g in enumerate(group))
        prompt = (
            "Şəkli diqqətlə oxu. Aşağıdakı hər sətir üçün YALNIZ şəkildə yazılı olana əsasən yoxla: bu ad (marka, həcm/çəki) "
            "və məhz bu qiymət şəkildə, eyni məhsulun yanında görünürmü?\n" + listing + "\n"
            'YALNIZ JSON massivi qaytar: [{"n": 1, "ok": true, "readPrice": 2.49}]. ok yalnız ad və qiymət şəkildən '
            "tam təsdiqlənirsə true; qiymət fərqlidirsə ok=false və readPrice-a şəkildə gördüyün qiyməti yaz. Təxmin etmə."
        )
        try:
            res = extract_json_array(gemini_text(client, [prompt, part]))
        except Exception:
            return []
        ok_idx = set()
        for r in res:
            if isinstance(r, dict) and r.get("ok") is True and isinstance(r.get("n"), int):
                ok_idx.add(r["n"] - 1)
        return [g for i, g in enumerate(group) if i in ok_idx]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for (url, group), good in zip(by_img.items(), pool.map(lambda kv: confirm(*kv), by_img.items())):
            dropped += len(group) - len(good)
            kept += good
    for it in kept:
        it.pop("_img", None)
    STATS["vision_dropped"] = dropped
    log.info("  [Vision təsdiqi] %d məhsul təsdiqləndi, %d təsdiqlənmədi və atıldı", len(kept), dropped)
    return kept


PDF_MAX_FILES = env_int("PDF_MAX_FILES", 3)
PDF_MAX_PAGES = env_int("PDF_MAX_PAGES", 10)


def collect_pdf_catalogs(blocks: list[dict]) -> None:
    """Mətnlərdə (Telegram/Facebook/Instagram/sayt) tapılan PDF kataloq/jurnalları səhifə-səhifə şəklə çevirib CATALOG_IMAGES-ə əlavə edir."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        log.warning("  [PDF] PyMuPDF yoxdur — PDF kataloqlar ötürüldü")
        return
    found: dict[str, str] = {}
    for b in blocks:
        market = b.get("market") or ""
        for m in re.finditer(r"https?://[^\s\"'<>)\]]+?\.pdf", json.dumps(b, ensure_ascii=False)):
            found.setdefault(m.group(0).replace("\\/", "/"), market)
    n = 0
    for url, market in found.items():
        if n >= PDF_MAX_FILES:
            break
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code != 200 or len(r.content) < 20000 or len(r.content) > 40_000_000:
                continue
            doc = fitz.open(stream=r.content, filetype="pdf")
            pages = 0
            for page in doc:
                if pages >= PDF_MAX_PAGES:
                    break
                pix = page.get_pixmap(dpi=150)
                CATALOG_IMAGES.append({"market": normalize_market(market) or market, "source": "PDF kataloq",
                                       "url": f"{url}#p{page.number + 1}", "bytes": pix.tobytes("jpeg")})
                pages += 1
            n += 1
            log.info("  [PDF] %s: %d səhifə", url, pages)
        except Exception as exc:
            log.warning("  [PDF xətası] %s: %s", url[:80], str(exc)[:100])


def download_image_bytes(url: str):
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200 or len(r.content) < 3000 or len(r.content) > 12_000_000:
            return None
        return r.content
    except Exception:
        return None


def split_catalog_images(client, today: str) -> list[dict]:
    """Kataloq/kollaj şəklindən məhsulların adını və qiymətini oxuyur. Kəsilmiş şəkil İSTİFADƏ OLUNMUR (keyfiyyətsizdir):
    şəkil sonradan internetdən axtarılır."""
    out = []
    for cat in CATALOG_IMAGES[:CATALOG_MAX_IMAGES]:
        raw = cat.get("bytes") or download_image_bytes(cat["url"])
        if not raw:
            continue
        try:
            Image.open(io.BytesIO(raw)).verify()
        except Exception:
            continue
        part = types.Part.from_bytes(data=raw, mime_type="image/jpeg") if types is not None else None
        prompt = (
            f"Bu {cat['market']} marketinin endirim kataloqu / kollaj şəklidir. Qiyməti (AZN / ₼) aydın görünən HƏR məhsulu ayrıca oxu. "
            "YALNIZ JSON massivi qaytar: "
            '[{"name": "marka və həcm/çəki ilə tam ad", "oldPrice": 3.5, "price": 2.49, "unit": "1 ədəd", "percent": 20, "endDate": "2026-12-31"}]. '
            "Üstündən xətt çəkilmiş rəqəm köhnə qiymət, qırmızı/vurğulanmış rəqəm yeni endirimli qiymətdir; böyük rəqəm manat, kiçik yuxarı rəqəm qəpikdir. "
            "Kart-üzvü ('OBA Plus', 'üzvlərinə') qiymətini price kimi yazma. Faiz yazılıbsa percent yaz. endDate yalnız şəkildə bu endirim üçün yazılıbsa (YYYY-MM-DD). "
            "Qiyməti aydın olmayan məhsulu yazma. Heç nə uydurma."
        )
        try:
            rows = [x for x in extract_json_array(gemini_text(client, [prompt, part])) if isinstance(x, dict) and x.get("name") and x.get("price")]
        except Exception as exc:
            log.warning("  [Kataloq bölmə xətası] %s: %s", cat["market"], str(exc)[:120])
            continue
        for r in rows[:40]:
            pr, op, pc = safe_float(r.get("price"), 0) or 0, safe_float(r.get("oldPrice"), 0) or 0, safe_float(r.get("percent"), 0) or 0
            if pr <= 0 or (op and pr >= op):
                continue
            if op and pc and abs(op * (1 - pc / 100.0) - pr) > max(0.06, pr * 0.03):
                STATS["price_rejected"] = STATS.get("price_rejected", 0) + 1
                continue
            out.append({"name": r["name"], "price": pr, "oldPrice": op or None, "unit": r.get("unit") or "1 ədəd",
                        "category": "Ərzaq", "market": cat["market"], "source": cat["source"] + " (kataloq)",
                        "endDate": r.get("endDate") or "", "imageUrl": "", "imgSrc": ""})
    log.info("  [Kataloq bölmə] %d şəkildən %d məhsul (ad+qiymət) oxundu", len(CATALOG_IMAGES[:CATALOG_MAX_IMAGES]), len(out))
    return out


AZ_MONTHS = {"yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7, "avqust": 8,
             "sentyabr": 9, "oktyabr": 10, "noyabr": 11, "dekabr": 12}
_MONTH_RE = "|".join(AZ_MONTHS)
_PRICE_RE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:₼|azn|man(?:at)?\b)", re.I)
_NOISE_WORDS = re.compile(r"\b(endirim\w*|aksiya\w*|yalnız|qiymət\w*|günün|təklif\w*|qənaət\w*|tarixi|hər gün|hergun və|bu gün|"
                          r"ələ keç|yeni|kampaniya\w*|əvəzinə|köhnə|yeni qiymət)\b", re.I)


def parse_end_date(text: str) -> str:
    """'26-30 noyabr' / '30 noyabrdək' kimi yazıdan bitmə tarixini (YYYY-MM-DD) çıxarır."""
    low = (text or "").lower()
    m = re.search(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(" + _MONTH_RE + r")", low)
    if not m:
        singles = re.findall(r"()(\d{1,2})\s*(" + _MONTH_RE + r")", low)
        m = None
        if singles:
            class _M:   # bir neçə tarix varsa (başlama + bitmə) sonuncusu bitmə tarixidir
                def __init__(self, g): self.g = g
                def group(self, i): return self.g[i - 1]
            m = _M(singles[-1])
    if not m:
        return ""
    day, month = int(m.group(2)), AZ_MONTHS[m.group(3)]
    now = datetime.now(timezone(timedelta(hours=4)))
    try:
        d = datetime(now.year, month, day, tzinfo=now.tzinfo)
    except ValueError:
        return ""
    if d < now - timedelta(days=60):
        d = d.replace(year=now.year + 1)
    return d.strftime("%Y-%m-%d")


def parse_start_date(text: str) -> str:
    """'26-30 noyabr' → 26 noyabr; '1 dekabrdan' → 1 dekabr kimi başlama tarixini (YYYY-MM-DD) çıxarır."""
    low = (text or "").lower()
    m = re.search(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*(" + _MONTH_RE + r")", low)
    if m:
        day, mon = int(m.group(1)), AZ_MONTHS[m.group(3)]
    else:
        m = re.search(r"(\d{1,2})\s*(" + _MONTH_RE + r")\w*\s*(?:-?(?:dan|dən|tarixindən|tarixindan))", low)
        if not m:
            return ""
        day, mon = int(m.group(1)), AZ_MONTHS[m.group(2)]
    now = datetime.now(timezone(timedelta(hours=4)))
    try:
        d = datetime(now.year, mon, day, tzinfo=now.tzinfo)
    except ValueError:
        return ""
    if d < now - timedelta(days=60):
        d = d.replace(year=now.year + 1)
    return d.strftime("%Y-%m-%d")


_BOILER_RE = re.compile(r"(?:^|\n|\.\s)\s*qeyd\s*:[^\n]*|\d+\s*(?:azn|₼|manat)\s*(?:və\s*üzəri|və\s*çox|-?\s*lik|-?\s*lıq|dan\s+çox)[^\n.]*|[^\n.]*lotereya[^\n.]*|[^\n.]*obaplus[^\n.]*", re.I)


def strip_boilerplate(text: str) -> str:
    """Kampaniya/lotereya/tətbiq haqqında ümumi qeydləri (məsələn, '10 AZN və üzəri alış-veriş') məhsul qiymətindən ayırır."""
    return _BOILER_RE.sub(" ", text or "").strip()


def parse_caption_item(text: str):
    """Paylaşımın yazısında BİR məhsulun adı və qiyməti açıq yazılıbsa onu qaytarır; yoxsa None (şəkildən oxunacaq)."""
    text = strip_boilerplate(text)
    if not text or len(text) < 6:
        return None
    prices = []
    for raw in _PRICE_RE.findall(text):
        try:
            v = float(raw.replace(",", "."))
        except ValueError:
            continue
        if 0.05 <= v <= 5000:
            prices.append(round(v, 2))
    uniq = sorted(set(prices))
    if not uniq or len(uniq) > 2:
        return None
    price = uniq[0]
    old = uniq[1] if len(uniq) == 2 and uniq[1] >= price * 1.05 else 0
    name = ""
    for line in text.split("\n"):
        ln = _PRICE_RE.sub(" ", line)
        ln = re.sub(r"https?://\S+|[#@]\S+|\d{1,2}\s*[-–—]\s*\d{1,2}\s*(" + _MONTH_RE + r")\S*|-\s*\d+\s*%|\d+\s*%\s*(?:endirim\w*|daha\s+qənaətli)", " ", ln, flags=re.I)
        ln = _NOISE_WORDS.sub(" ", ln)
        ln = re.sub(r"[^\w%.,/+()\- əƏıİöÖüÜçÇşŞğĞ]", " ", ln)
        ln = re.sub(r"\s+", " ", ln).strip(" -–—.,:;()")
        if len(ln) >= 5 and sum(c.isalpha() for c in ln) >= 4:
            name = ln[:80]
            break
    if not name:
        return None
    # "təmiz" yazı: qısa məhsul adı, ümumi/kampaniya sözləri yox, qiymət yazıda təkdir — Gemini yoxlaması tələb olunmur
    words = name.split()
    generic = re.search(r"market\w*|şəbəkə\w*|alış|çox|xüsusi|hədiyyə|bonus|kart|kampaniya|mağaza|filial|ünvan|yeni açıl|nömrə|tel\b|whatsapp", name, re.I)
    clean = 1 <= len(words) <= 7 and not generic and len(prices) <= 2 and (old == 0 or old > price)
    return {"name": name, "price": price, "oldPrice": old, "unit": "1 ədəd", "category": "Ərzaq",
            "endDate": parse_end_date(text), "startDate": parse_start_date(text), "clean": clean}


def check_caption_items(client, items: list[dict]) -> list[dict]:
    """Yazıdan oxunan məhsulların şəklini yoxlayır: tək məhsul şəkli olmalıdır (kataloq/kollaj yox).
    Şəkildə qiymət oxunursa yazıdakı ilə üst-üstə düşməlidir. Gemini işləmirsə yazıdakı tək qiymət kifayət edir."""
    kept = []

    def check(it):
        part = download_image_part(it["_img"])
        if part is None:
            return None
        if client is None:
            return it
        prompt = (
            "Bu şəkil bir marketin paylaşımıdır. Şəkildə YALNIZ BİR məhsulun öz fotosu varsa single=true; bir neçə məhsul, "
            "kataloq, vərəq, qiymət cədvəli və ya reklam kollajıdırsa single=false. Şəkildə görünən endirimli (yeni) qiyməti "
            'oxu. YALNIZ JSON qaytar: {"single": true, "price": 9.99, "name": "şəkildəki tam ad"}. Qiymət görünmürsə price=null. Uydurma.'
        )
        try:
            raw = gemini_text(client, [prompt, part])
            m = re.search(r"\{.*\}", raw or "", re.S)
            res = json.loads(m.group(0)) if m else {}
        except Exception:
            return it   # Gemini cavab vermədi (kvota) — yazıdakı tək qiymətə güvənirik
        if res.get("single") is False:
            if all(c["url"] != it["_img"] for c in CATALOG_IMAGES):
                CATALOG_IMAGES.append({"market": it["market"], "source": it["source"], "url": it["_img"]})
            return None
        read_price = safe_float(res.get("price"), 0) or 0
        if read_price and abs(read_price - it["price"]) > 0.011 and abs(read_price - (it.get("oldPrice") or -1)) > 0.011:
            log.info("  [Yazı≠şəkil] %s: yazıda %.2f, şəkildə %.2f — kataloq bölməsinə göndərildi", it["name"][:40], it["price"], read_price)
            if all(c["url"] != it["_img"] for c in CATALOG_IMAGES):   # yazıdakı rəqəm məhsulun qiyməti deyil; şəkil çox güman ki, kolajdır
                CATALOG_IMAGES.append({"market": it["market"], "source": it["source"], "url": it["_img"]})
            return None
        return it

    with ThreadPoolExecutor(max_workers=3) as pool:
        for it in pool.map(check, items):
            if it:
                kept.append(it)
    return kept


def extract_from_promo_images_vision(client, candidates: list[dict]) -> list[dict]:
    """Aksiya şəkillərini (Instagram, Facebook, Telegram, sayt) Gemini Vision ilə oxuyur; bir sorğuda bir neçə şəkil."""
    seen, by_market = set(), {}
    captions: dict[str, str] = {}
    from_caption = []
    # hər market/mənbə (kanal, səhifə) bərabər paya malik olsun: növbə ilə seç
    _buckets: dict = {}
    for _c in candidates:
        _buckets.setdefault((_c.get("market"), _c.get("source")), []).append(_c)
    candidates = []
    while any(_buckets.values()):
        for _k in list(_buckets):
            if _buckets[_k]:
                candidates.append(_buckets[_k].pop(0))
    for cand in candidates:
        url = cand.get("imageUrl")
        if not url or url in seen or len(seen) >= VISION_MAX:
            continue
        seen.add(url)
        cap = (cand.get("caption") or "").strip()
        parsed = parse_caption_item(cap) if cap else None
        if parsed:   # ad və qiymət paylaşımın yazısında var — şəkildən oxumağa ehtiyac yoxdur
            parsed.update({"market": cand["market"], "source": cand.get("source", "Sosial şəbəkə"), "_img": url})
            from_caption.append(parsed)
            continue
        if cap:
            captions[url] = cap[:300]
        by_market.setdefault((cand["market"], cand.get("source", "Sosial şəbəkə")), []).append(url)
    for (_m, _src), _urls in by_market.items():
        log.info("  [Mənbə] %s / %s: şəkildən oxunacaq %d paylaşım", _m, _src, len(_urls))
    _cap_by = {}
    for _it in from_caption:
        _cap_by[(_it.get("market"), _it.get("source"))] = _cap_by.get((_it.get("market"), _it.get("source")), 0) + 1
    for (_m, _src), _n in _cap_by.items():
        log.info("  [Mənbə] %s / %s: yazıdan oxunan %d paylaşım", _m, _src, _n)
    clean_items = [it for it in from_caption if it.get("clean")]
    check_items = [it for it in from_caption if not it.get("clean")]
    cap_ok = clean_items + (check_caption_items(client, check_items) if check_items else [])
    log.info("  [Yazıdan oxuma] %d paylaşım Gemini yoxlaması olmadan birbaşa götürüldü, %d yoxlandı", len(clean_items), len(check_items))
    for it in cap_ok:
        it["imageUrl"], it["imgSrc"] = it.pop("_img"), "source"
    STATS["caption_items"] = len(cap_ok)
    log.info("  [Yazıdan oxuma] %d paylaşımda ad+qiymət yazıdan götürüldü, %d təsdiqləndi", len(from_caption), len(cap_ok))

    extracted = []   # yalnız şəkildən oxunanlar (yazıdan oxunanlar cap_ok-dadır)
    for (market, source), urls in by_market.items():
        for i in range(0, len(urls), VISION_PER_CALL):
            chunk = urls[i:i + VISION_PER_CALL]
            loaded = [(u, download_image_part(u)) for u in chunk]
            loaded = [(u, part) for u, part in loaded if part is not None]
            if not loaded:
                continue
            hints = "".join(f"\nŞəkil {n} paylaşımının yazısı: {strip_boilerplate(captions[u])[:300]}" for n, (u, _) in enumerate(loaded, 1) if u in captions)
            prompt = (
                f"Bu {len(loaded)} şəkil {market} marketinin ({source}) paylaşımlarıdır; şəkillər 1-dən {len(loaded)}-ə nömrələnib "
                "(verilmə sırası ilə). Şəkillərdə konkret qiyməti (AZN / ₼) görünən bütün məhsulları YALNIZ JSON massivi kimi qaytar: "
                '[{"imageIndex": 1, "layout": "single", "name": "...", "category": "Ərzaq", "oldPrice": 3.5, "price": 2.49, '
                '"unit": "1 ədəd", "percent": 22, "startDate": "2026-12-25", "endDate": "2026-12-31"}]. '
                "layout: şəkildə YALNIZ BİR məhsul (onun öz fotosu, yanında qiymət yazısı ola bilər) varsa \"single\"; "
                "bir neçə məhsul, kataloq, vərəq, qiymət cədvəli, banner və ya reklam kollajıdırsa \"catalog\". "
                "name — marka və həcm/çəki ilə tam ad (şəkildə oxuduğun kimi). startDate (başlama) və endDate (bitmə) yalnız şəkildə yazılıbsa (YYYY-MM-DD). "
                "Əvvəlcə paylaşımın yazısındakı ad, qiymət və tarixə bax (aşağıda verilib), yazıda yoxdursa şəkildəki yazıları oxu. "
                "QİYMƏT ETİKETLƏRİ: üstündən xətt çəkilmiş (adətən sarı) rəqəm = köhnə qiymət (oldPrice); qırmızı etiketdəki rəqəm = yeni endirimli qiymət (price). "
                "Etiketdə böyük rəqəm manatın tam hissəsi, yuxarıdakı kiçik rəqəm qəpikdir (böyük 1, kiçik 79 → 1.79). "
                "Yaşıl 'OBA Plus / üzvlərinə endirimli qiymət' və ya '2 ədədi' etiketi kart-üzvü qiymətidir: onu price kimi YAZMA. "
                "'22% daha qənaətli' kimi yazı varsa percent=22 yaz. Məhsulun adı adətən şəklin sol tərəfindəki yazıdır (marka + növ + çəki/həcm, məs. 'Ankor Kərə yağı 82.9% 1 kq'); "
                "unit-i də oradan götür ('1 kq', '380 q'). Lotereya, tətbiq, kampaniya və ya ümumi reklam banneri (qiymət etiketi olmayan) şəkildən heç nə çıxarma. "
                "endDate yalnız MƏHSULUN endirimi üçün yazılıbsa; lotereya/kampaniya tarixi və '10 AZN və üzəri alış-veriş' kimi şərtlər məhsul qiyməti deyil. "
                "Qiymət yoxdursa [] qaytar. Heç nə uydurma." + hints
            )
            try:
                STATS["vision_images"] += len(loaded)
                rows = [x for x in extract_json_array(gemini_text(client, [prompt] + [p for _, p in loaded]))
                        if isinstance(x, dict) and x.get("name") and x.get("price")]
                _ok_rows = []
                for x in rows:   # etiketdəki faiz ilə köhnə/yeni qiymət uyğun gəlməlidir, yoxsa oxuma səhvdir
                    pr, op, pc = safe_float(x.get("price"), 0) or 0, safe_float(x.get("oldPrice"), 0) or 0, safe_float(x.get("percent"), 0) or 0
                    if op and pr >= op:
                        STATS["price_rejected"] = STATS.get("price_rejected", 0) + 1
                        continue
                    if op and pc and abs(op * (1 - pc / 100.0) - pr) > max(0.06, pr * 0.03):
                        log.info("  [Qiymət uyğunsuzluğu] %s: köhnə %.2f, yeni %.2f, %s%% — atıldı", str(x.get("name"))[:40], op, pr, pc)
                        STATS["price_rejected"] = STATS.get("price_rejected", 0) + 1
                        continue
                    x.pop("percent", None)
                    _ok_rows.append(x)
                rows = _ok_rows
                per_img = {}
                for x in rows:
                    per_img[x.get("imageIndex")] = per_img.get(x.get("imageIndex"), 0) + 1
                for item in rows:
                    item["market"], item["source"] = market, source
                    idx = item.pop("imageIndex", None)
                    if isinstance(idx, int) and 1 <= idx <= len(loaded):
                        item["_img"] = loaded[idx - 1][0]
                    layout = str(item.pop("layout", "") or "").lower()
                    if not (layout == "single" and per_img.get(idx) == 1 and isinstance(idx, int)
                            and 1 <= idx <= len(loaded)):
                        STATS["catalog_skipped"] = STATS.get("catalog_skipped", 0) + 1
                        if isinstance(idx, int) and 1 <= idx <= len(loaded):
                            cu = loaded[idx - 1][0]
                            if all(c["url"] != cu for c in CATALOG_IMAGES):
                                CATALOG_IMAGES.append({"market": market, "source": source, "url": cu})
                        continue   # kataloq şəkli bütöv olaraq istifadə olunmur, aşağıda hissələrə bölünür
                    item["imageUrl"] = loaded[idx - 1][0]   # paylaşımın öz şəkli = məhsulun şəkli
                    item["imgSrc"] = "source"
                    STATS["single_photo"] = STATS.get("single_photo", 0) + 1
                    extracted.append(item)
            except Exception as exc:
                log.warning("  [Vision xətası] %s: %s", market, str(exc)[:160])
    extracted = cap_ok + confirm_vision_items(client, extracted)
    log.info("  [Vision AI] %d şəkildən %d məhsul oxundu (%d kataloq/kollaj məhsulu atıldı)", STATS["vision_images"], len(extracted), STATS.get("catalog_skipped", 0))
    return extracted


# --------------------------------------------------------------------------- məhsulların təhlili
def pick_svg_type(category: str, name: str) -> str:
    low = (name + " " + category).lower()
    rules = [
        ("paper", ("kağız", "kagiz", "salfet", "dəsmal", "desmal", "tualet", "pampers", "bez")),
        ("butter", ("kərə yağ", "kere yag", "kərə", "kere")),
        ("yogurt", ("qatıq", "qatiq", "yoqurt", "kefir", "ayran", "qaymaq")),
        ("milk", ("süd", "sud", "pendir")),
        ("oil", ("yağ", "yag")),
        ("tea", ("çay", "cay", "qəhvə", "kofe", "nescafe")),
        ("soap", ("sabun", "fairy", "bulaşıq", "şampun", "gel")),
        ("detergent", ("yuyucu", "ariel", "persil", "təmiz", "tide")),
        ("sweet", ("şokolad", "konfet", "peçenye", "vafli", "tort", "biskvit", "şirniyyat", "dondurma")),
        ("fruit", ("alma", "armud", "banan", "pomidor", "xiyar", "kartof", "soğan", "meyvə", "tərəvəz", "limon",
                   "portağal", "üzüm", "nar", "kök", "badımcan", "bibər")),
        ("meat", ("ət", "toyuq", "sosis", "kolbasa", "balıq", "fileto", "kotlet")),
        ("bread", ("çörək", "lavaş", "xəmir", "un ", "makaron", "düyü", "yarma")),
        ("drink", ("kola", "cola", "su ", "şirə", "içki", "fanta", "limonad", "sprite", "pepsi")),
    ]
    for svg, kws in rules:
        if any(k in low for k in kws):
            return svg
    return "bag"


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
            "3. startDate (başlama) və endDate (bitmə) yalnız mətndə yazılıbsa (YYYY-MM-DD), yoxdursa boş burax.\n"
            "4. imageUrl yalnız veb sayt blokundakı `images` siyahısından tək məhsulun şəkli ola bilər; "
            "toplu kataloq/post şəkillərini yazma.\n"
            "5. market: Bravo, Araz, Bazarstore, Rahat, OBA, AL Market, Grandmart, Bolmart.\n"
            "6. YALNIZ JSON massivi qaytar (maksimum 60 məhsul). Sahələr: name, category "
            '("Süd məhsulları"|"Ərzaq"|"İçkilər"|"Təmizlik"|"Şirniyyat"), market, '
            'source ("Instagram"|"Facebook"|"Veb sayt"|"Telegram kanalı"), oldPrice, price, unit, imageUrl, startDate, endDate.\n\n'
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
            STATS["expired_dropped"] = STATS.get("expired_dropped", 0) + 1
            return None  # bitmiş endirim
        end_date = raw_end
    start_date = str(it.get("startDate") or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date) or (end_date and start_date > end_date):
        start_date = ""
    cat = guess_category(name)   # qaydalarla (Gemini-yə güvənmədən) hər məhsula kateqoriya təyin olunur
    if cat == "Digər":
        g_cat = clean_text(it.get("category"), 30)
        if g_cat in ALLOWED_CATEGORIES:
            cat = g_cat
    key = normalize_key(name)
    if not key:
        return None
    img = clean_text(it.get("imageUrl"), 600)
    return {
        "key": key, "name": name, "category": cat, "market": normalize_market(it.get("market") or ""),
        "source": clean_text(it.get("source") or "Veb sayt", 30), "price": price, "oldPrice": old,
        "unit": clean_text(it.get("unit") or "1 ədəd", 25), "imageUrl": img if img.startswith("https://") else "",
        "endDate": end_date, "startDate": start_date, "imgSrc": (it.get("imgSrc") or "") if img.startswith("https://") else "",
    }


def size_sig(name: str):
    """Çəki/həcmi qram/ml-ə çevirir (500 qr, 0.5 kq, 1 L ...)."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s?(kq|kg|qr|gr|g|q|ml|litr|lt|l)\b", (name or "").lower().replace("ı", "i"))
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return round(v * 1000) if m.group(2) in ("kq", "kg", "litr", "lt", "l") else round(v)


def core_tokens(name: str) -> set:
    folded = (name or "").lower().replace("ı", "i").replace("i̇", "i")
    return {t for t in name_tokens(folded) if not t.isdigit() and t not in ("litr", "lt", "kiloqram", "qram", "paket", "ədəd", "bahalı", "yağlı", "yagli", "ml", "gr")}


def same_product(a: dict, b: dict) -> bool:
    ta, tb = core_tokens(a["name"]), core_tokens(b["name"])
    if not ta or not tb:
        return False
    sa, sb = size_sig(a["name"]) or size_sig(a.get("unit", "")), size_sig(b["name"]) or size_sig(b.get("unit", ""))
    if sa and sb and sa != sb:
        return False
    inter = len(ta & tb)
    if sa and sb and min(len(ta), len(tb)) >= 2 and inter / float(min(len(ta), len(tb))) >= 0.99:
        return True   # ölçü eynidir və qısa ad uzun adın içindədir
    return inter / float(len(ta | tb)) >= 0.7


def group_entries(entry_list: list[dict]) -> dict:
    """Fərqli marketlərdə eyni məhsulu ad+çəki üzrə uyğunlaşdırır (dəqiq eyni yazılış tələb olunmur)."""
    groups: list[list[dict]] = []
    for e in entry_list:
        for g in groups:
            if all(x["market"] != e["market"] for x in g) and same_product(g[0], e):
                g.append(e)
                break
        else:
            groups.append([e])
    out = {}
    for g in groups:
        gk = g[0]["key"]
        for e in g:
            out[f"{e['key']}|{e['market']}"] = g
    return out


def bazar_regular_lookup(name: str):
    """Bazarstore axtarışında eyni məhsulun cari (endirimli və ya adi) qiymətini tapır."""
    words = sorted(core_tokens(name), key=len, reverse=True)
    probe = {"name": name, "unit": ""}
    tried = set()
    for n in (3, 2):
        q = " ".join(words[:n])
        if not q or q in tried:
            continue
        tried.add(q)
        try:
            items = parse_bazarstore_page(fetch_html("https://bazarstore.az/search?q=" + quote(q), timeout=15))
        except Exception:
            continue
        for it in items[:10]:
            if same_product(probe, {"name": it["name"], "unit": it.get("unit", "")}) or \
               (len(core_tokens(name) & core_tokens(it["name"])) >= 2 and (size_sig(name) in (None, size_sig(it["name"])))):
                return it
    return None


def build_products(raw_items: list[dict], prev_items: list[dict], image_cache: dict, today: str, client=None, catalogs: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Təmizləyir, təkrarları birləşdirir, marketlər arası müqayisə qurur, tarixçə və şəkilləri yeniləyir."""
    # 1. Təmizlə və (məhsul, market) üzrə ən ucuz təklifi saxla
    entries: dict[str, dict] = {}
    for it in raw_items:
        e = clean_item(it, today)
        if not e or e["market"] in EXCLUDED_MARKETS:
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
        seen_d = str(p.get("seenDate") or "")
        keep_noend = (not end) and seen_d and seen_d >= (datetime.now(timezone(timedelta(hours=4))) - timedelta(days=7)).strftime("%Y-%m-%d")
        if ((end and end >= today) or keep_noend) and hk not in entries and k and market not in EXCLUDED_MARKETS:
            entries[hk] = {
                "key": k, "name": clean_text(p.get("name")), "category": p.get("category", "Ərzaq"), "market": market,
                "source": clean_text(p.get("source") or "Veb sayt", 30), "price": safe_float(p.get("price"), 0),
                "oldPrice": safe_float(p.get("oldPrice"), 0) or 0, "unit": clean_text(p.get("unit") or "1 ədəd", 25),
                "imageUrl": p.get("imageUrl") or "", "imgSrc": p.get("imgSrc") or "", "endDate": end, "startDate": p.get("startDate") or "", "carried": True, "seenDate": p.get("seenDate") or today,
            }
            carried += 1
    if carried:
        log.info("  Bitmə tarixi çatmamış %d əvvəlki endirim saxlanıldı", carried)

    def _src_rank(x):   # Telegram ən yüksək üstünlük, sonra Instagram, sonra Facebook
        sname = str(x.get("source") or "")
        return 0 if sname.startswith("Telegram") else 1 if sname.startswith("Instagram") else 2

    # 3b. Eyni marketin eyni məhsulunun təkrar (üst-üstə düşən) paylaşımlarını birləşdir
    def _toks(e):
        return set(re.findall(r"[a-z0-9əöüğçşı]{2,}", (e.get("name") or "").lower()))
    ent_list = list(entries.items())
    drop_keys = set()
    by_mk: dict[str, list] = {}
    for hk_, e_ in ent_list:
        by_mk.setdefault(e_["market"], []).append((hk_, e_))
    for mk_, lst in by_mk.items():
        for i in range(len(lst)):
            if lst[i][0] in drop_keys:
                continue
            for j in range(i + 1, len(lst)):
                if lst[j][0] in drop_keys:
                    continue
                a_, b_ = lst[i][1], lst[j][1]
                ta, tb = _toks(a_), _toks(b_)
                if not ta or not tb:
                    continue
                jac = len(ta & tb) / len(ta | tb)
                same_price = abs((a_["price"] or 0) - (b_["price"] or 0)) < 0.006
                cross = _src_rank(a_) != _src_rank(b_)   # Telegram ↔ Instagram/Facebook: eyni paylaşım təkrarlanmasın
                if jac >= 0.9 or (jac >= 0.7 and same_price) or (cross and ((jac >= 0.6 and same_price) or jac >= 0.8)):
                    def score(x):
                        return (-_src_rank(x), bool(x.get("imageUrl")), bool(x.get("endDate")), -(x.get("price") or 0), len(x.get("name") or ""))
                    loser = lst[j][0] if score(a_) >= score(b_) else lst[i][0]
                    drop_keys.add(loser)
    for k_ in drop_keys:
        entries.pop(k_, None)
    if drop_keys:
        log.info("  Təkrar paylaşımlar birləşdirildi: %d", len(drop_keys))

    # 4. Marketlər arası qruplaşdırma
    groups = group_entries(list(entries.values()))
    # Başqa marketlərin TAM kataloqundan (adi + endirimli cari qiymət) eyni məhsulu tap
    extra_comps: dict[str, list] = {}
    seen_groups = set()
    todo_old = []
    for hk0, g in groups.items():
        if id(g) in seen_groups:
            continue
        seen_groups.add(id(g))
        for mk in ("Bazarstore", "Araz"):
            if any(x["market"] == mk for x in g):
                continue
            idx = (catalogs or {}).get(mk)
            if idx is not None and idx.entries:
                f_ = idx.find(g[0]["name"])
                if f_:
                    extra_comps.setdefault(hk0, []).append({"market": mk, "price": f_["price"], "isPromo": bool(f_["old"]), "name": clean_text(f_["name"], 90)})
            elif mk == "Bazarstore" and not TG_ONLY:
                todo_old.append((hk0, g))
    if todo_old:   # kataloq alınmayıbsa köhnə üsul: Bazarstore axtarışı
        with ThreadPoolExecutor(max_workers=8) as pool:
            for (hk0, g), found in zip(todo_old[:BAZAR_LOOKUP_MAX], pool.map(lambda t: bazar_regular_lookup(t[1][0]["name"]), todo_old[:BAZAR_LOOKUP_MAX])):
                if found:
                    extra_comps.setdefault(hk0, []).append({"market": "Bazarstore", "price": found["price"], "isPromo": bool(found.get("oldPrice")), "name": clean_text(found.get("name") or "", 90)})
    log.info("  Tam kataloqdan müqayisə: %d məhsul qrupu üçün əlavə qiymət tapıldı", len(extra_comps))

    # 5. Şəkillər: öz şəkli → əvvəlki şəkil → keş → internet axtarışı
    entry_list = list(entries.values())

    def check(e):
        u = e["imageUrl"]
        if u.startswith(CROP_RAW_BASE):   # bu işləmədə yaradılıb, workflow sonda repoya yükləyəcək
            return e, os.path.exists(os.path.join(CROP_DIR, u[len(CROP_RAW_BASE):]))
        return e, bool(u) and verify_image_url(u, 200)

    for e in entry_list:   # kataloqdan kəsilmiş şəkillər keyfiyyətsizdir: istifadə olunmur, əvəzinə internetdən axtarılır
        if str(e.get("imageUrl") or "").startswith(CROP_RAW_BASE):
            e["imageUrl"], e["imgSrc"] = "", ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        for e, ok in pool.map(check, [e for e in entry_list if e["imageUrl"]]):
            if not ok:
                e["imageUrl"] = ""
    pool_imgs = [(name_tokens(e["name"]), e["imageUrl"]) for e in entry_list if e["imageUrl"]]

    def similar_image(e):
        """Eyni məhsulun başqa marketdəki öz şəkli (ad uyğunluğu ≥75%) varsa onu götür."""
        tk = name_tokens(e["name"])
        best, score = "", 0.0
        for t2, url in pool_imgs:
            if not tk or not t2:
                continue
            j = len(tk & t2) / float(len(tk | t2))
            if j > score:
                best, score = url, j
        return best if score >= 0.75 else ""

    searched = 0
    for e in sorted(entry_list, key=lambda x: x["price"]):
        if e["imageUrl"]:
            e["imgSrc"] = e.get("imgSrc") or "source"
            continue
        prev = prev_index.get(f"{e['key']}|{e['market']}")
        if (prev and prev.get("imageUrl") and not str(prev["imageUrl"]).startswith(CROP_RAW_BASE) and prev.get("imgSrc") in ("source", "verified", "matched")
                and (not re.search(r"fbcdn|cdninstagram|scontent", prev["imageUrl"]) or image_alive(prev["imageUrl"]))):
            e["imageUrl"], e["imgSrc"] = prev["imageUrl"], prev["imgSrc"]   # yalnız yoxlanmış şəkil təkrar istifadə olunur
        elif (image_cache.get(e["key"]) or "").startswith("https://"):
            e["imageUrl"], e["imgSrc"] = image_cache[e["key"]], "verified"
        elif similar_image(e):
            e["imageUrl"], e["imgSrc"] = similar_image(e), "matched"
        elif searched < IMAGE_SEARCH_MAX:
            url = find_high_quality_product_image(e["name"], image_cache, client, today)
            if url:
                e["imageUrl"], e["imgSrc"] = url, "verified"
            searched += 1

    log.info("  Şəkilsiz qalan məhsul: %d / %d (internetdən tapılan: %d, axtarış: %d)", sum(1 for e in entry_list if not e["imageUrl"]), len(entry_list),
             sum(1 for e in entry_list if e.get("imgSrc") == "verified"), searched)

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
            drops.append({"id": "p_" + short_hash(hk), "name": e["name"], "market": e["market"], "oldPrice": safe_float(prev["price"]), "price": e["price"]})

        comps = [{"market": g["market"], "price": g["price"], "isPromo": True, "name": clean_text(g["name"], 90), "hk": f"{g['key']}|{g['market']}"} for g in groups[hk]]
        have = {c["market"] for c in comps}
        for k2 in [hk] + [f"{x['key']}|{x['market']}" for x in groups[hk]]:
            for ex in extra_comps.get(k2, []):
                if ex["market"] not in have:
                    comps.append(ex)
                    have.add(ex["market"])
        comps.sort(key=lambda c: c["price"])
        match = f"{len(comps)} marketdə müqayisə edildi" if len(comps) > 1 else "Bu marketin təklifidir"
        products.append({
            "id": "p_" + short_hash(hk),
            "name": e["name"], "category": e["category"], "market": e["market"], "source": e["source"],
            "oldPrice": e["oldPrice"], "price": e["price"], "unit": e["unit"], "imageUrl": e["imageUrl"], "imgSrc": e.get("imgSrc", ""),
            "endDate": e["endDate"], "startDate": e.get("startDate", ""), "svgType": pick_svg_type(e["category"], e["name"]),
            "iconBg": "from-emerald-50 to-teal-100 dark:from-emerald-950/60 dark:to-teal-900/40",
            "seenDate": (prev or {}).get("seenDate") or e.get("seenDate") or today,
            "aiMatch": match, "comparisons": comps, "history30": hist, "histKey": hk, "histDate": today,
        })

    id_by_hk = {p_["histKey"]: p_["id"] for p_ in products}
    for p_ in products:
        for c_ in p_["comparisons"]:
            h_ = c_.pop("hk", None)
            if h_ and h_ in id_by_hk and c_["market"] != p_["market"]:
                c_["pid"] = id_by_hk[h_]

    def discount(p):
        return (p["oldPrice"] - p["price"]) / p["oldPrice"] if p["oldPrice"] else 0

    products = [p for p in products if p.get("imageUrl") and not (p.get("endDate") and str(p["endDate"]) < today)]   # şəkilsiz və vaxtı bitmiş məhsul əlavə olunmur
    products.sort(key=lambda p: (-discount(p), p["price"]))
    return products[:MAX_PRODUCTS], drops


# --------------------------------------------------------------------------- cashback
CB_GOOD = re.compile(r"kesbek|keshbek|keşbək|cashback|bonus|market|kart", re.I)
CB_BAD = re.compile(r"atm|location|branch|filial|news|xeber|blog|youtube|facebook|instagram|vakans|career|\.pdf$", re.I)


def find_bank_page(bank: str, domain: str) -> str:
    """Bankın rəsmi cashback səhifəsini axtarır; yalnız öz domenində və cashback sözü olan ünvanı qəbul edir."""
    if DDGS is None:
        return ""
    best, best_score = "", 0
    for q in (f"{bank} market keşbək kart", f"{bank} cashback kartlar", f"{bank} keşbək"):
        try:
            time.sleep(0.6)
            results = DDGS().text(q, max_results=8) or []
        except Exception:
            continue
        for r in results:
            href = r.get("href") or ""
            host = urlparse(href).netloc.lower()
            if not href.startswith("https://") or domain not in host or CB_BAD.search(href):
                continue
            score = len(CB_GOOD.findall(href)) * 2 + (1 if re.search(r"kesbek|cashback", href, re.I) else 0)
            if urlparse(href).path.strip("/") in ("", "az", "en", "ru"):
                score = 0   # ana səhifə
            if score > best_score:
                best, best_score = href, score
        if best_score >= 3:
            break
    return best


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


def send_telegram_report(text: str) -> None:
    """Gecə hesabatını Telegram bot ilə göndərir (TELEGRAM_BOT_TOKEN və TELEGRAM_CHAT_ID varsa)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log.info("  [Hesabat] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID yoxdur — hesabat göndərilmədi")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}, timeout=20)
        log.info("  [Hesabat] Telegram: HTTP %s", r.status_code)
    except Exception as exc:
        log.warning("  [Hesabat xətası] %s", type(exc).__name__)


def send_price_drop_notifications(db, drops: list[dict], dry: bool) -> int:
    """Sevimli məhsulunun qiyməti düşən istifadəçilərə telefon bildirişi göndərir (FCM)."""
    if not drops:
        return 0
    if dry or db is None or messaging is None:
        log.info("  [Bildiriş] %d qiymət düşməsi var (sınaq rejimi/messaging yoxdur — göndərilmədi)", len(drops))
        return 0
    by_id = {d["id"]: d for d in drops if d.get("id")}
    sent = 0
    try:
        users = list(db.collection("users").stream())
    except Exception as exc:
        log.warning("  [Bildiriş] istifadəçilər oxunmadı: %s", str(exc)[:120])
        return 0
    for u in users:
        d = u.to_dict() or {}
        tokens = [t for t in (d.get("fcmTokens") or []) if isinstance(t, str) and t]
        prefs = d.get("notifPrefs") or {}
        if not tokens or prefs.get("priceDrop") is False:
            continue
        mine = [by_id[f] for f in (d.get("favorites") or []) if f in by_id]
        if not mine:
            continue
        mine.sort(key=lambda x: (x["oldPrice"] - x["price"]) / x["oldPrice"] if x.get("oldPrice") else 0, reverse=True)
        lines = [f"{x['name']} ({x['market']}): {x['oldPrice']:.2f} → {x['price']:.2f} ₼" for x in mine[:3]]
        if len(mine) > 3:
            lines.append(f"və daha {len(mine) - 3} məhsul")
        try:
            msg = messaging.MulticastMessage(
                tokens=tokens[:20],
                notification=messaging.Notification(title="Sevimli məhsulunun qiyməti düşdü 📉", body="\n".join(lines)),
                webpush=messaging.WebpushConfig(fcm_options=messaging.WebpushFCMOptions(link="https://qenaet.pages.dev/")),
            )
            resp = messaging.send_each_for_multicast(msg)
            sent += resp.success_count
            dead = [tokens[i] for i, r in enumerate(resp.responses) if not r.success and getattr(r.exception, "code", "") in ("NOT_FOUND", "UNREGISTERED", "INVALID_ARGUMENT")]
            if dead:
                u.reference.update({"fcmTokens": firestore.ArrayRemove(dead)})
        except Exception as exc:
            log.warning("  [Bildiriş xətası] %s", str(exc)[:120])
    log.info("  [Bildiriş] %d bildiriş göndərildi", sent)
    STATS["push_sent"] = sent
    return sent


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
    apify_token = os.environ.get("APIFY_TOKEN", "").strip() if os.environ.get("USE_APIFY", "1").strip() != "0" else ""

    import faulthandler
    faulthandler.dump_traceback_later(1500, repeat=True)  # donma olarsa, hara ilişdiyini jurnala yazır
    try:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=90000))
    except Exception:
        client = genai.Client(api_key=api_key)
    pick_models(client)
    db = None if dry else init_firestore()
    if os.environ.get("CLEAR_CATALOG", "").strip() == "1":
        if db is None:
            log.warning("Sınaq rejimində silmə yoxdur.")
            return
        now_l = datetime.now(timezone(timedelta(hours=4))).strftime("%d.%m.%Y %H:%M")
        write_doc(db, "daily_discounts", {"updatedLabel": f"Təmizləndi: {now_l}", "date": "", "count": 0, "items": [], "cleared": True}, False)
        n_del = 0
        for d_ in db.collection("catalog").stream():
            if d_.id.startswith("market_prices_") or d_.id in ("daily_changes",):
                d_.reference.delete()
                n_del += 1
        log.info("Bütün məhsullar silindi (daily_discounts boşaldıldı, %d kataloq sənədi silindi).", n_del)
        return
    now = datetime.now(BAKU_TZ)
    today, now_label = now.strftime("%Y-%m-%d"), now.strftime("%d.%m.%Y %H:%M")
    started = time.time()

    prev_discounts = read_doc(db, "daily_discounts")
    prev_items = [p for p in prev_discounts.get("items", []) if isinstance(p, dict)]
    prev_cashback = [c for c in read_doc(db, "daily_cashback").get("items", []) if isinstance(c, dict)]
    cache_doc = read_doc(db, "image_cache").get("map", {})
    image_cache = dict(cache_doc) if cache_doc.get("_v") == IMAGE_CACHE_VERSION else {}
    image_cache["_v"] = IMAGE_CACHE_VERSION
    assets = read_doc(db, "brand_assets")
    existing_logos = dict(assets.get("logos", {})) if assets.get("version") == LOGO_VERSION else {}

    # əvvəlki tapıntılar hələ də açılırmı? Ölü olanlar silinib yenidən axtarılır
    card_images = {k: v for k, v in dict(assets.get("cards", {})).items() if assets.get("version") == LOGO_VERSION and isinstance(v, str) and v.startswith("https://") and image_alive(v)}
    existing_logos = {k: v for k, v in existing_logos.items() if isinstance(v, str) and v.startswith("https://") and image_alive(v)}
    log.info("1. Market və bank loqoları yoxlanılır...")
    brand_logos = collect_brand_logos(existing_logos, client)
    write_doc(db, "brand_assets", {"updatedLabel": f"Yeniləndi: {now_label}", "version": LOGO_VERSION, "logos": brand_logos, "cards": card_images}, dry)

    if TG_ONLY:
        log.info("2. YALNIZ sosial mənbələr (Telegram → Instagram → Facebook): saytlar, kataloqlar və PDF-lər ötürülür.")
        web_blocks, structured, web_vision = [], [], []
        socials = {k: dict(v) for k, v in DEFAULT_SOCIAL_URLS.items()}
        tg_channels = dict(TELEGRAM_CHANNELS)
        for pair in (os.environ.get("TELEGRAM_EXTRA") or "").split(","):
            if ":" in pair:
                m_, slug_ = pair.split(":", 1)
                tg_channels[m_.strip()] = slug_.strip().lstrip("@")
        catalog_future = None
    else:
        log.info("2. Market saytları skan edilir (endirim səhifələri avtomatik tapılır)...")
        web_blocks, structured, socials, tg_channels, web_vision = scrape_market_websites()
        log.info("2b. Bazarstore və Araz endirim məlumatları (API/səhifələr)...")
        structured += with_retry(scrape_bazarstore, "Bazarstore", default=[]) + with_retry(scrape_araz, "Araz", default=[])
        log.info("2c. Bazarstore və Araz TAM kataloqu arxa planda yığılır (müqayisə üçün)...")
        catalog_pool = ThreadPoolExecutor(max_workers=1)
        catalog_future = catalog_pool.submit(scrape_full_catalogs)

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
    vision_items = extract_from_promo_images_vision(client, web_vision + ig_vision + fb_vision + tg_vision)
    if not TG_ONLY:   # PDF kataloqlar yalnız köhnə (sayt) rejimində oxunur
        collect_pdf_catalogs(web_blocks + ig_blocks + fb_blocks + tg_blocks)
    try:
        catalog_items = split_catalog_images(client, today)
    except Exception as exc:
        log.warning("  [Kataloq bölmə] ötürüldü: %s", str(exc)[:160])
        catalog_items = []
    vision_items += catalog_items
    text_blocks = web_blocks + ig_blocks[:40] + fb_blocks[:40] + tg_blocks
    gemini_items = gemini_extract_products(client, text_blocks) if text_blocks else []
    # Yalnız saytda həqiqətən olan və adı məhsula uyğun gələn şəkillər qəbul edilir (Gemini şəkil uydura bilər)
    page_images = {im["url"]: im.get("alt", "") for b in web_blocks for im in b.get("images", [])}
    for it in structured:
        it["imgSrc"] = "source"
    for it in gemini_items:
        url, alt = it.get("imageUrl"), page_images.get(it.get("imageUrl"))
        if url in page_images and tokens_overlap(it.get("name", ""), alt or "", 1) and alt:
            it["imgSrc"] = "source"
        else:
            it["imageUrl"] = ""
    raw_items = structured + vision_items + gemini_items
    try:   # diaqnostika: seçilmiş marketin (default OBA) mənbə paylaşımları və bot nəyi necə oxuyub
        dbg_m = os.environ.get("DEBUG_MARKET", "OBA")
        os.makedirs("out", exist_ok=True)
        with open("out/debug_market.json", "w", encoding="utf-8") as f_:
            json.dump({"market": dbg_m,
                       "candidates": [{k_: v_ for k_, v_ in c_.items()} for c_ in (web_vision + ig_vision + fb_vision + tg_vision) if normalize_market(c_.get("market", "")) == dbg_m],
                       "items": [i_ for i_ in raw_items if normalize_market(i_.get("market", "")) == dbg_m]}, f_, ensure_ascii=False, indent=1, default=str)
    except Exception as exc_:
        log.warning("  debug yazılmadı: %s", str(exc_)[:100])
    log.info("  Xam məhsul: sayt/API %d + Vision %d + Gemini %d", len(structured), len(vision_items), len(gemini_items))

    catalogs = {}
    try:
        full = catalog_future.result(timeout=max(60, CATALOG_BUDGET_SEC + 120 - int(time.time() - started))) if catalog_future else {}
    except Exception as exc:
        log.warning("  [Tam kataloq] gözləmə xətası: %s", str(exc)[:120])
        full = {}
    for mk, items_ in full.items():
        doc_name = "market_prices_" + mk.lower()
        if items_:
            write_doc(db, doc_name, {"updatedLabel": f"Yeniləndi: {now_label}", "date": today, "count": len(items_), "items": items_[:CATALOG_MAX_ITEMS]}, dry)
        elif not TG_ONLY:   # alınmadısa dünənki kataloqdan istifadə et
            items_ = [x for x in read_doc(db, doc_name).get("items", []) if isinstance(x, dict) and x.get("n")]
            if items_:
                log.info("  [Tam kataloq] %s: bu gün alınmadı, əvvəlki kataloq istifadə olunur (%d)", mk, len(items_))
        if items_:
            catalogs[mk] = CatalogIndex(mk, items_)
            STATS["catalog_" + mk.lower()] = len(items_)
    products, drops = build_products(raw_items, prev_items, image_cache, today, client, catalogs)
    with_img = sum(1 for p in products if p.get("imageUrl"))
    multi = sum(1 for p in products if len(p["comparisons"]) > 1)
    log.info("Yekun məhsul: %d (şəkilli: %d, çox marketdə müqayisəli: %d, qiyməti düşən: %d)", len(products), with_img, multi, len(drops))

    # artıq istifadə olunmayan kəsilmiş şəkilləri sil (repo kiçik qalsın)
    used = {p_["imageUrl"][len(CROP_RAW_BASE):] for p_ in products if str(p_.get("imageUrl", "")).startswith(CROP_RAW_BASE)}
    if products and os.path.isdir(CROP_DIR):
        for f_ in os.listdir(CROP_DIR):
            if f_.endswith(".jpg") and f_ not in used:
                try:
                    os.remove(os.path.join(CROP_DIR, f_))
                except OSError:
                    pass
    if products:
        write_doc(db, "daily_discounts", {"updatedLabel": f"Yeniləndi: {now_label}", "date": today, "count": len(products), "items": products}, dry)
        write_doc(db, "image_cache", {"map": image_cache}, dry)
    else:
        log.warning("Yeni endirim tapılmadı — mövcud kataloq qorundu.")

    log.info("6. Bank cashback-ləri yenilənir...")
    cards = analyze_cashbacks_with_gemini(client)
    card_t0 = time.time()
    cards_src = dict(assets.get("cardsSrc") or {}) if assets.get("version") == LOGO_VERSION else {}
    cards_tried = dict(assets.get("cardsTried") or {}) if assets.get("version") == LOGO_VERSION else {}
    for c in cards[:30]:
        ckey = f"{c['bank']}|{c['cardName']}"
        in_time = time.time() - card_t0 < 420
        recently_tried = cards_tried.get(ckey, "") >= (datetime.now(timezone(timedelta(hours=4))) - timedelta(days=7)).strftime("%Y-%m-%d")
        if cards_src.get(ckey) != "official" and not recently_tried and in_time:
            u = official_card_image(client, c["bank"], c["cardName"], c.get("sourceUrl", ""))
            cards_tried[ckey] = today
            if u:
                card_images[ckey], cards_src[ckey] = u, "official"
                log.info("  [Kart şəkli] %s — %s: rəsmi saytdan tapıldı", c["bank"], c["cardName"])
        if not card_images.get(ckey) and in_time:
            u = search_verified_brand_image(client, c["bank"], "card", extra=c["cardName"])
            if u:
                card_images[ckey], cards_src[ckey] = u, "web"
                log.info("  [Kart şəkli] %s — %s: dəqiq şəkil tapıldı", c["bank"], c["cardName"])
        if card_images.get(ckey):
            c["cardImage"] = card_images[ckey]
    STATS["card_official"] = sum(1 for v in cards_src.values() if v == "official")
    if not dry:
        write_doc(db, "brand_assets", {"updatedLabel": f"Yeniləndi: {now_label}", "version": LOGO_VERSION, "logos": brand_logos, "cards": card_images,
                                  "cardsSrc": cards_src, "cardsTried": cards_tried}, dry)
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

    log.info("7. Qiyməti düşən sevimli məhsullar üçün bildirişlər...")
    send_price_drop_notifications(db, drops, dry)

    log.info("— Yekun —  vaxt: %ds | %s", int(time.time() - started), json.dumps(STATS, ensure_ascii=False))
    report = (
        f"Qənaət botu — {now_label}\n"
        f"Məhsul: {len(products)} (şəkilli {with_img}, çox marketdə müqayisəli {multi}, qiyməti düşən {len(drops)})\n"
        f"Bazarstore kataloq: {STATS.get('catalog_bazarstore', 0)} | Araz kataloq: {STATS.get('catalog_araz', 0)}\n"
        f"Paylaşım: Instagram {STATS.get('ig_posts', 0)}, Facebook {STATS.get('fb_posts', 0)}, Telegram {STATS.get('tg_posts', 0)}\n"
        f"Şəkildən oxunan (təsdiqli): {STATS.get('single_photo', 0)}, təsdiqlənməyib atılan: {STATS.get('vision_dropped', 0)}\n"
        f"Kataloqdan kəsilən: təsdiq {len([1 for p_ in products if str(p_.get('imageUrl','')).startswith(CROP_RAW_BASE)])}, rədd {STATS.get('crop_rejected', 0)}\n"
        f"Bank kartı: {len(cards)} | Bildiriş: {STATS.get('push_sent', 0)}\n"
        f"Müddət: {int((time.time() - started) / 60)} dəq"
    )
    if time.time() - _RUN_START > GEMINI_DEADLINE_SEC:
        report += "\nDiqqət: Gemini vaxt limiti keçdi, bəzi mərhələlər yarımçıq qala bilər."
    if not products:
        report += "\nDiqqət: yeni endirim tapılmadı, əvvəlki kataloq qorundu."
    send_telegram_report(report)
    if total_sources == 0:
        log.error("Heç bir mənbədən məlumat alınmadı (şəbəkə və ya saytlar dəyişib).")
        send_telegram_report("Qənaət botu: HEÇ BİR mənbədən məlumat alınmadı (şəbəkə və ya saytlar dəyişib).")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        send_telegram_report(f"Qənaət botu XƏTA ilə dayandı: {type(exc).__name__}: {str(exc)[:300]}")
        raise
