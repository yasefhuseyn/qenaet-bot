"""
Qənaət — Gündəlik Avtomatik Endirim və Cashback Yeniləmə Botu
GitHub Actions üzərindən hər səhər avtomatik işə düşür və Firestore (qenaet-c705e) bazasını yeniləyir.
"""
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

try:
    from google import genai
except ImportError:
    genai = None

BAKU_TZ = timezone(timedelta(hours=4))

# İctimai Telegram kanalları (istədiyiniz market kanalını əlavə edə bilərsiniz)
TELEGRAM_CHANNELS = {
    "Bravo": "bravosupermarketaz",
    "Araz": "arazsupermarket",
    "OBA": "obamarketaz",
    "AL Market": "almarketaz",
    "Bazarstore": "bazarstore_az",
}


def fetch_public_telegram_posts(channel_slug: str, limit: int = 8) -> list[str]:
    """İctimai Telegram kanalının veb görünüşündən (t.me/s/...) son paylaşımları oxuyur."""
    url = f"https://t.me/s/{channel_slug}"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        posts = []
        for div in soup.select(".tgme_widget_message_text")[-limit:]:
            text = div.get_text(" ", strip=True)
            if text and len(text) > 15:
                posts.append(text[:500])
        return posts
    except Exception as exc:
        print(f"Telegram oxuma xətası ({channel_slug}): {exc}")
        return []


def parse_discounts_with_gemini(raw_blocks: list[dict]) -> list[dict]:
    """Pulsuz Gemini API ilə xam endirim mətnlərini strukturlaşdırılmış JSON məhsullara çevirir."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or genai is None or not raw_blocks:
        return []

    client = genai.Client(api_key=api_key)
    prompt = (
        "Aşağıdakı Azərbaycan marketlərinin son endirim paylaşımlarından konkret qiyməti olan "
        "məhsulları çıxar və YALNIZ JSON massivi qaytar. Hər elementdə bu sahələr olsun: "
        'id, name, category ("Süd məhsulları"|"Ərzaq"|"İçkilər"|"Təmizlik"|"Şirniyyat"), '
        'market, source ("Telegram kanalı"), oldPrice (rəqəm), price (rəqəm), unit, aiMatch.\n\n'
        + json.dumps(raw_blocks[:25], ensure_ascii=False)
    )
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw_text = (resp.text or "").strip()
        match = re.search(r"\[.*\]", raw_text, re.DOTALL)
        if not match:
            return []
        items = json.loads(match.group(0))
        cleaned = []
        for idx, item in enumerate(items):
            if not item.get("name") or not item.get("price"):
                continue
            price = float(item["price"])
            old_price = float(item.get("oldPrice") or round(price * 1.18, 2))
            cleaned.append({
                "id": f"auto_{idx+1}",
                "name": str(item["name"])[:90],
                "category": item.get("category", "Ərzaq"),
                "market": item.get("market", "Bravo"),
                "source": item.get("source", "Telegram kanalı"),
                "oldPrice": old_price,
                "price": price,
                "unit": item.get("unit", "1 ədəd"),
                "imageUrl": "",
                "svgType": "milk",
                "iconBg": "from-emerald-50 to-teal-100 dark:from-emerald-950/60 dark:to-teal-900/40",
                "aiMatch": item.get("aiMatch", "AI avtomatik uyğunlaşdırma"),
                "comparisons": [{"market": item.get("market", "Bravo"), "price": price, "isPromo": True}],
                "history30": [old_price, price],
            })
        return cleaned
    except Exception as exc:
        print(f"Gemini analiz xətası: {exc}")
        return []


def init_firestore():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT gizli açarı tapılmadı.")
    cred_dict = json.loads(sa_json)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred_dict))
    return firestore.client()


def main():
    now_baku = datetime.now(BAKU_TZ).strftime("%d.%m.%Y %H:%M")
    raw_blocks = []
    for market, slug in TELEGRAM_CHANNELS.items():
        posts = fetch_public_telegram_posts(slug)
        for p in posts:
            raw_blocks.append({"market": market, "text": p})

    print(f"Toplanan xam paylaşım sayı: {len(raw_blocks)}")
    parsed_items = parse_discounts_with_gemini(raw_blocks)
    print(f"AI ilə çıxarılan endirim sayı: {len(parsed_items)}")

    db = init_firestore()
    if parsed_items:
        db.collection("catalog").document("daily_discounts").set({
            "updatedLabel": f"Yeniləndi: {now_baku}",
            "items": parsed_items,
        })
        print("Firestore catalog/daily_discounts uğurla yeniləndi.")
    else:
        print("Yeni məhsul tapılmadığı üçün mövcud kataloq qorundu.")


if __name__ == "__main__":
    main()
