from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import pytesseract
import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
EXPENSES_DATA_SOURCE_ID = os.getenv(
    "NOTION_EXPENSES_DATA_SOURCE_ID",
    "0e3a0f1e-1619-4005-bdef-8a85cc67fbe6",
)
WEEKS_DATA_SOURCE_ID = os.getenv(
    "NOTION_WEEKS_DATA_SOURCE_ID",
    "839aa182-17d4-4ae7-a01a-35dfde7cf371",
)
NOTION_API_VERSION = os.getenv("NOTION_API_VERSION", "2026-03-11")
ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
WEEKLY_BUDGET = float(os.getenv("WEEKLY_BUDGET", "20"))
AUTO_CONFIRM = os.getenv("AUTO_CONFIRM", "false").lower() in {"1", "true", "yes", "oui"}
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Madrid"))
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "spa+fra+eng")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger("ticket-bot")
# httpx logs Telegram URLs, which contain the bot token. Never emit them at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

NOTION = requests.Session()
NOTION.headers.update(
    {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }
)

IGNORE_ITEM_WORDS = {
    "total",
    "sous total",
    "sous-total",
    "tva",
    "taxe",
    "carte",
    "cb",
    "visa",
    "mastercard",
    "especes",
    "espèces",
    "rendu",
    "montant",
    "a payer",
    "à payer",
    "net a payer",
    "net à payer",
    "paiement",
    "ticket",
    "facture",
    "solde",
    "remise",
    "economies",
    "économies",
    "subtotal",
    "sous-total",
    "importe total",
    "total a pagar",
    "base imponible",
    "iva",
    "cambio",
    "efectivo",
    "tarjeta",
    "entrega",
    "imp",
    "suma",
}

CATEGORY_KEYWORDS = {
    "Alimentation": [
        "pain", "lait", "oeuf", "œuf", "riz", "pate", "pâtes", "viande",
        "poulet", "poisson", "banane", "fruit", "legume", "légume", "fromage",
        "yaourt", "huile", "eau", "jus", "cafe", "café", "sucre", "farine",
        "tomate", "oignon", "biscuit", "chocolat", "cereal", "céréale",
        "arroz", "cebolla", "datil", "dátil", "leche", "huevo", "pan",
        "pollo", "pescado", "platano", "plátano", "fruta", "verdura",
        "queso", "yogur", "aceite", "agua", "zumo", "café", "azucar",
        "azúcar", "harina", "galleta", "chocolate", "pasta", "carne",
        "ciruela",
    ],
    "Hygiène": [
        "savon", "shampoing", "dentifrice", "deodorant", "déodorant",
        "papier toilette", "mouchoir", "gel douche", "brosse",
        "jabon", "jabón", "champu", "champú", "pasta de dientes",
        "desodorante", "papel higienico", "papel higiénico",
    ],
    "Maison": [
        "lessive", "eponge", "éponge", "nettoyant", "javel", "sac poubelle",
        "ampoule", "vaisselle", "essuie tout", "essuie-tout",
        "detergente", "lejia", "lejía", "limpiador", "esponja",
        "bolsa basura", "lavavajillas",
    ],
    "Transport": [
        "essence", "gazole", "diesel", "parking", "peage", "péage", "ticket bus",
        "tram", "train",
        "gasolina", "gasoleo", "gasóleo", "aparcamiento", "autobus", "autobús",
    ],
    "Loisir": [
        "cinema", "cinéma", "jeu", "livre", "sport", "streaming",
        "cine", "juego", "libro", "deporte",
    ],
}

PRICE_RE = re.compile(
    r"(?<!\d)(-?(?:\d{1,3}(?:[ .]\d{3})+|\d+)[,.]\d{2})"
    r"\s*(?:€|eur)?\s*(?:[A-Z])?\s*$",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip().lower()


NORMALIZED_IGNORE_ITEM_WORDS = {normalize(word) for word in IGNORE_ITEM_WORDS}


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        LOGGER.warning("%s invalide ; valeur par défaut %s utilisée", name, default)
        value = default
    return max(minimum, min(maximum, value))


def parse_price(raw: str) -> float:
    cleaned = re.sub(r"[^\d,.-]", "", raw).replace(" ", "")
    if not cleaned or cleaned in {"-", ".", ","}:
        raise ValueError(f"Prix invalide : {raw!r}")

    comma = cleaned.rfind(",")
    dot = cleaned.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_separator = "," if comma > dot else "."
        thousands_separator = "." if decimal_separator == "," else ","
        cleaned = cleaned.replace(thousands_separator, "")
        cleaned = cleaned.replace(decimal_separator, ".")
    elif comma >= 0:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif dot >= 0:
        cleaned = cleaned.replace(",", "")

    return round(float(cleaned), 2)


def categorize(name: str) -> str:
    normalized = normalize(name)
    for category, words in CATEGORY_KEYWORDS.items():
        if any(normalize(word) in normalized for word in words):
            return category
    return "Autre"


def _deskew(gray: np.ndarray) -> np.ndarray:
    inverted = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    points = np.column_stack(np.where(inverted > 0))[:, ::-1].astype(np.float32)
    if len(points) < 100:
        return gray

    angle = cv2.minAreaRect(points)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < 0.25 or abs(angle) > 12:
        return gray

    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def preprocess_images(path: str) -> list[tuple[str, np.ndarray]]:
    image = cv2.imread(path)
    if image is None:
        raise ValueError("Impossible de lire l’image.")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    longest_side = max(height, width)
    target_long_side = _bounded_int_env("OCR_TARGET_LONG_SIDE", 1600, 1000, 2400)
    scale = min(2.5, target_long_side / longest_side)
    if abs(scale - 1.0) > 0.01:
        interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interpolation)

    gray = _deskew(gray)
    denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    adaptive = cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        9,
    )
    return [("contrast", clahe), ("otsu", otsu), ("adaptive", adaptive)]


def preprocess_image(path: str) -> np.ndarray:
    """Backward-compatible best single preprocessing variant."""
    return preprocess_images(path)[-1][1]


def _text_quality(text: str) -> float:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    price_lines = sum(bool(PRICE_RE.search(line)) for line in lines)
    total_lines = sum("total" in normalize(line) for line in lines)
    date_lines = sum(bool(DATE_RE.search(line)) for line in lines)
    readable = sum(char.isalnum() or char.isspace() for char in text)
    ratio = readable / max(len(text), 1)
    return price_lines * 8 + total_lines * 10 + date_lines * 4 + min(len(lines), 40) + ratio * 5


def read_receipt_text(path: str) -> tuple[str, str]:
    available = set(pytesseract.get_languages(config=""))
    requested = OCR_LANGUAGES.split("+")
    languages = "+".join(language for language in requested if language in available)
    if not languages:
        languages = "eng"

    variants = dict(preprocess_images(path))
    attempt_plan = [
        ("adaptive", 6),
        ("contrast", 4),
        ("otsu", 6),
        ("contrast", 6),
        ("adaptive", 4),
        ("otsu", 4),
    ]
    max_attempts = _bounded_int_env("OCR_MAX_ATTEMPTS", 2, 1, len(attempt_plan))
    pass_timeout = _bounded_int_env("OCR_PASS_TIMEOUT_SECONDS", 35, 10, 120)

    candidates: list[tuple[float, str, str]] = []
    timed_out = 0
    for variant_name, psm in attempt_plan[:max_attempts]:
        method = f"{variant_name}/psm{psm}"
        started = time.monotonic()
        LOGGER.info("OCR tentative=%s timeout=%ss", method, pass_timeout)
        try:
            text = pytesseract.image_to_string(
                variants[variant_name],
                lang=languages,
                config=f"--oem 1 --psm {psm} -c preserve_interword_spaces=1",
                timeout=pass_timeout,
            )
        except RuntimeError as exc:
            timed_out += 1
            LOGGER.warning(
                "OCR tentative=%s interrompue après %.1fs: %s",
                method,
                time.monotonic() - started,
                exc,
            )
            continue
        score = _text_quality(text)
        candidates.append((score, method, text))
        LOGGER.info(
            "OCR tentative=%s terminée en %.1fs score=%.1f",
            method,
            time.monotonic() - started,
            score,
        )

    if not candidates:
        if timed_out:
            raise TimeoutError(
                "La lecture du ticket a pris trop de temps. "
                "Recadre le ticket seul, puis renvoie la photo."
            )
        raise RuntimeError("Le moteur de lecture n’a produit aucun résultat.")

    score, method, text = max(candidates, key=lambda candidate: candidate[0])
    LOGGER.info("OCR retenu=%s score=%.1f candidats=%s", method, score, len(candidates))
    return text, method


def detect_purchase_date(lines: list[str]) -> date:
    for line in lines:
        match = DATE_RE.search(line)
        if not match:
            continue
        day, month, year = map(int, match.groups())
        if year < 100:
            year += 2000
        try:
            candidate = date(year, month, day)
            if date(2000, 1, 1) <= candidate <= date.today() + timedelta(days=1):
                return candidate
        except ValueError:
            continue
    return datetime.now(TIMEZONE).date()


def detect_merchant(lines: list[str]) -> str:
    merchant_markers = (
        "aldi",
        "alcampo",
        "auchan",
        "carrefour",
        "dia",
        "eroski",
        "intermarche",
        "leclerc",
        "lidl",
        "mercadona",
        "monoprix",
        "supermercado",
        "supermarket",
    )
    candidates: list[tuple[str, str]] = []
    for line in lines[:20]:
        clean = re.sub(r"[^A-Za-zÀ-ÿ0-9 '&.-]", " ", line)
        clean = re.sub(r"\s+", " ", clean).strip()
        normalized = normalize(clean)
        if (
            len(clean) >= 3
            and any(char.isalpha() for char in clean)
            and not DATE_RE.search(clean)
            and not PRICE_RE.search(clean)
            and not any(word in normalized for word in NORMALIZED_IGNORE_ITEM_WORDS)
            and not re.match(r"^\d{1,2}\s+\d{2}\b", clean)
        ):
            candidates.append((clean[:80], normalized))

    for clean, normalized in candidates:
        if any(marker in normalized for marker in merchant_markers):
            return clean
    if candidates:
        return candidates[0][0]
    return "Magasin non identifié"


def detect_total(lines: list[str]) -> float | None:
    candidates: list[tuple[int, int, float]] = []
    for index, line in enumerate(lines):
        normalized = normalize(line)
        if "subtotal" in normalized or "sous total" in normalized:
            continue
        priority = 0
        if any(
            marker in normalized
            for marker in (
                "net a payer",
                "total a payer",
                "montant total",
                "importe total",
                "total eur",
            )
        ):
            priority = 2
        elif re.search(r"\btotal\b", normalized):
            priority = 1
        if not priority:
            continue
        match = PRICE_RE.search(line)
        if match:
            candidates.append((priority, index, parse_price(match.group(1))))

    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2] if candidates else None


def _clean_item_name(raw_name: str) -> str:
    name = raw_name.strip(" .:-*|_")
    name = re.sub(r"^\d+\s*[x×]\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(
        r"\s+\d+[,.]\d{2,3}\s*[x×]\s*\d+(?:[,.]\d+)?\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(
        r"\s+\d+[,.]\d{2,3}\s*(?:kg|g|l|cl|ml|u|ud|uds)\b.*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(
        r"\s+\d+[,.]\d{2,3}\s*(?:€|eur)?\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"\s+", " ", name).strip(" .:-*|_")
    return name


def detect_items(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in lines:
        match = PRICE_RE.search(line)
        if not match:
            continue

        name = _clean_item_name(line[: match.start()])
        normalized = normalize(name)

        if len(name) < 2:
            continue
        if any(
            word == normalized or normalized.startswith(word + " ")
            for word in NORMALIZED_IGNORE_ITEM_WORDS
        ):
            continue
        if any(
            word in normalized
            for word in (
                "tva",
                "iva",
                "total",
                "a payer",
                "a pagar",
                "paiement",
                "carte bancaire",
                "tarjeta",
                "base imponible",
            )
        ):
            continue
        if re.match(r"^[A-Z]\s+\d+\s*%", name, flags=re.IGNORECASE):
            continue

        price = parse_price(match.group(1))
        if price == 0 or abs(price) > 10000:
            continue

        items.append(
            {
                "name": name[:120],
                "price": price,
                "type": categorize(name),
            }
        )
    return items


def _closest_subset(items: list[dict[str, Any]], target: float) -> list[dict[str, Any]] | None:
    if len(items) > 28 or target <= 0:
        return None
    target_cents = round(target * 100)
    states: dict[int, tuple[int, ...]] = {0: ()}
    for index, item in enumerate(items):
        cents = round(float(item["price"]) * 100)
        if cents <= 0 or cents > target_cents + 5:
            continue
        additions: dict[int, tuple[int, ...]] = {}
        for subtotal, selected in states.items():
            new_total = subtotal + cents
            if new_total <= target_cents + 5 and new_total not in states:
                additions[new_total] = selected + (index,)
        states.update(additions)

    close = [value for value in states if value and abs(value - target_cents) <= 5]
    if not close:
        return None
    best = min(close, key=lambda value: abs(value - target_cents))
    return [items[index] for index in states[best]]


def reconcile_items(
    items: list[dict[str, Any]],
    total: float | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if total is None:
        return items, ["Total non détecté : somme des lignes utilisée."]
    if not items:
        return items, warnings

    item_sum = round(sum(float(item["price"]) for item in items), 2)
    difference = round(total - item_sum, 2)
    if abs(difference) <= 0.05:
        if abs(difference) >= 0.01:
            items = items + [{"name": "Arrondi du ticket", "price": difference, "type": "Autre"}]
        return items, warnings

    if difference > 0:
        warnings.append(f"{difference:.2f} € du ticket n’ont pas pu être attribués à un produit.")
        return items + [
            {
                "name": "Article(s) non identifié(s)",
                "price": difference,
                "type": "Autre",
            }
        ], warnings

    subset = _closest_subset(items, total)
    if subset:
        warnings.append("Des lignes récapitulatives en double ont été écartées.")
        return subset, warnings

    warnings.append("Le détail était incohérent avec le total ; seul le total fiable a été conservé.")
    return [
        {
            "name": "Total du ticket (détail incertain)",
            "price": total,
            "type": "Autre",
        }
    ], warnings


def extract_receipt(path: str, receipt_fingerprint: str) -> dict[str, Any]:
    text, ocr_method = read_receipt_text(path)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]

    if not lines:
        raise ValueError("Aucun texte détecté. Reprends la photo bien à plat et avec plus de lumière.")

    merchant = detect_merchant(lines)
    purchase_date = detect_purchase_date(lines)
    total = detect_total(lines)
    items = detect_items(lines)
    warnings: list[str] = []

    if not any(DATE_RE.search(line) for line in lines):
        warnings.append("Date non détectée : date d’aujourd’hui utilisée.")
    if merchant == "Magasin non identifié":
        warnings.append("Magasin non identifié.")

    if not items and total is None:
        raise ValueError(
            "Je n’ai détecté ni produits ni total. Reprends une photo plus nette du ticket complet."
        )

    if not items and total is not None:
        items = [{"name": f"Ticket {merchant}", "price": total, "type": "Autre"}]

    items, reconciliation_warnings = reconcile_items(items, total)
    warnings.extend(reconciliation_warnings)
    item_sum = round(sum(float(item["price"]) for item in items), 2)

    final_total = round(total if total is not None else item_sum, 2)

    return {
        "merchant": merchant,
        "date": purchase_date.isoformat(),
        "items": items,
        "total": final_total,
        "fingerprint": f"ticket:{receipt_fingerprint}",
        "raw_text": text[:1800],
        "ocr_method": ocr_method,
        "warnings": warnings,
        "confidence": "élevée" if not warnings else "à vérifier",
    }


def notion_request(method: str, endpoint: str, **kwargs) -> dict[str, Any]:
    url = f"https://api.notion.com/v1{endpoint}"
    response = NOTION.request(method, url, timeout=30, **kwargs)
    if not response.ok:
        raise RuntimeError(f"Erreur Notion {response.status_code}: {response.text}")
    return response.json()


def find_or_create_week(purchase_date: date) -> str:
    monday = purchase_date - timedelta(days=purchase_date.weekday())
    sunday = monday + timedelta(days=6)
    title = f"Semaine du {monday.strftime('%d/%m/%Y')} au {sunday.strftime('%d/%m/%Y')}"

    result = notion_request(
        "POST",
        f"/data_sources/{WEEKS_DATA_SOURCE_ID}/query",
        json={
            "filter": {
                "property": "Début",
                "date": {"equals": monday.isoformat()},
            },
            "page_size": 1,
        },
    )

    if result.get("results"):
        return result["results"][0]["id"]

    page = notion_request(
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": WEEKS_DATA_SOURCE_ID,
            },
            "properties": {
                "Semaine": {
                    "type": "title",
                    "title": [{"type": "text", "text": {"content": title}}],
                },
                "Début": {"type": "date", "date": {"start": monday.isoformat()}},
                "Fin": {"type": "date", "date": {"start": sunday.isoformat()}},
                "Budget": {"type": "number", "number": WEEKLY_BUDGET},
            },
        },
    )
    return page["id"]


def calculate_week_spent(week_page_id: str) -> float:
    total = 0.0
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {
            "filter": {
                "and": [
                    {"property": "Semaine", "relation": {"contains": week_page_id}},
                    {"property": "Statut", "select": {"equals": "Acheté"}},
                ]
            },
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        result = notion_request(
            "POST",
            f"/data_sources/{EXPENSES_DATA_SOURCE_ID}/query",
            json=body,
        )
        for page in result.get("results", []):
            value = page.get("properties", {}).get("Prix réel", {}).get("number")
            if value is not None:
                total += float(value)
        if not result.get("has_more"):
            return round(total, 2)
        cursor = result.get("next_cursor")


def receipt_already_exists(fingerprint: str) -> bool:
    result = notion_request(
        "POST",
        f"/data_sources/{EXPENSES_DATA_SOURCE_ID}/query",
        json={
            "filter": {
                "property": "Notes",
                "rich_text": {"contains": fingerprint},
            },
            "page_size": 1,
        },
    )
    return bool(result.get("results"))


def create_expense(item: dict[str, Any], receipt: dict[str, Any], week_page_id: str) -> None:
    notes = (
        f"{receipt['fingerprint']} · Ajout automatique Telegram. "
        f"Total ticket : {receipt['total']:.2f} €"
    )
    notion_request(
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": EXPENSES_DATA_SOURCE_ID,
            },
            "properties": {
                "Produit / Achat": {
                    "type": "title",
                    "title": [{"type": "text", "text": {"content": item["name"]}}],
                },
                "Date": {
                    "type": "date",
                    "date": {"start": receipt["date"]},
                },
                "Type": {
                    "type": "select",
                    "select": {"name": item["type"]},
                },
                "Prix réel": {
                    "type": "number",
                    "number": item["price"],
                },
                "Magasin": {
                    "type": "rich_text",
                    "rich_text": [
                        {"type": "text", "text": {"content": receipt["merchant"]}}
                    ],
                },
                "Statut": {
                    "type": "select",
                    "select": {"name": "Acheté"},
                },
                "Semaine": {
                    "type": "relation",
                    "relation": [{"id": week_page_id}],
                },
                "Notes": {
                    "type": "rich_text",
                    "rich_text": [{"type": "text", "text": {"content": notes}}],
                },
            },
        },
    )


def commit_receipt(receipt: dict[str, Any]) -> dict[str, float | bool]:
    if receipt_already_exists(receipt["fingerprint"]):
        return {"duplicate": True, "spent": 0.0, "remaining": 0.0}

    purchase_date = date.fromisoformat(receipt["date"])
    week_page_id = find_or_create_week(purchase_date)

    for item in receipt["items"]:
        create_expense(item, receipt, week_page_id)

    new_spent = calculate_week_spent(week_page_id)

    return {
        "duplicate": False,
        "spent": new_spent,
        "remaining": round(WEEKLY_BUDGET - new_spent, 2),
    }


def is_allowed(update: Update) -> bool:
    if not ALLOWED_TELEGRAM_USER_ID:
        return True
    user = update.effective_user
    return bool(user and str(user.id) == ALLOWED_TELEGRAM_USER_ID)


def receipt_summary(receipt: dict[str, Any]) -> str:
    lines = [
        f"🧾 {receipt['merchant']}",
        f"📅 {receipt['date']}",
        "",
    ]

    max_items = 25
    for item in receipt["items"][:max_items]:
        lines.append(f"• {item['name']} — {item['price']:.2f} € [{item['type']}]")
    if len(receipt["items"]) > max_items:
        lines.append(f"… et {len(receipt['items']) - max_items} autre(s) ligne(s)")

    lines.extend(["", f"Total détecté : {receipt['total']:.2f} €"])
    if receipt.get("warnings"):
        lines.append("")
        lines.append("⚠️ À vérifier :")
        lines.extend(f"• {warning}" for warning in receipt["warnings"])
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else "inconnu"
    await update.message.reply_text(
        "Envoie-moi une photo nette et complète du ticket.\n"
        "Je vais lire les produits, les ajouter dans Notion et calculer le reste sur 20 €.\n\n"
        f"Ton identifiant Telegram est : {user_id}"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Accès refusé.")
        return

    message = update.message
    if not message or not message.photo:
        return

    status = await message.reply_text("🔎 Lecture du ticket…")
    photo = message.photo[-1]
    telegram_file = await context.bot.get_file(photo.file_id)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            temp_path = tmp.name
        await telegram_file.download_to_drive(custom_path=temp_path)

        receipt = await asyncio.to_thread(
            extract_receipt,
            temp_path,
            photo.file_unique_id,
        )

        if AUTO_CONFIRM:
            result = await asyncio.to_thread(commit_receipt, receipt)
            if result["duplicate"]:
                await status.edit_text("⚠️ Ce ticket existe déjà dans Notion.")
                return
            remaining = float(result["remaining"])
            warning = "🔴 Budget dépassé." if remaining < 0 else "🟢 Budget respecté."
            await status.edit_text(
                receipt_summary(receipt)
                + f"\n\n✅ Ajouté dans Notion.\n"
                + f"Dépensé cette semaine : {float(result['spent']):.2f} €\n"
                + f"Reste : {remaining:.2f} €\n{warning}"
            )
            return

        pending_id = uuid.uuid4().hex[:12]
        pending = context.application.bot_data.setdefault("pending_receipts", {})
        pending[pending_id] = {
            "receipt": receipt,
            "user_id": update.effective_user.id,
        }

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Ajouter à Notion", callback_data=f"ok:{pending_id}"),
                    InlineKeyboardButton("❌ Annuler", callback_data=f"cancel:{pending_id}"),
                ]
            ]
        )
        await status.edit_text(receipt_summary(receipt), reply_markup=keyboard)

    except Exception as exc:
        LOGGER.exception("Traitement du ticket impossible")
        await status.edit_text(f"❌ {exc}")
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("Accès refusé.")
        return

    action, pending_id = query.data.split(":", 1)
    pending = context.application.bot_data.setdefault("pending_receipts", {})
    entry = pending.get(pending_id)

    if not entry:
        await query.edit_message_text("Cette demande a expiré. Renvoie la photo du ticket.")
        return
    if entry["user_id"] != update.effective_user.id:
        await query.edit_message_text("Cette demande ne t’appartient pas.")
        return

    if action == "cancel":
        pending.pop(pending_id, None)
        await query.edit_message_text("❌ Ticket annulé, rien n’a été ajouté.")
        return

    receipt = entry["receipt"]
    await query.edit_message_text("⏳ Ajout dans Notion…")

    try:
        result = await asyncio.to_thread(commit_receipt, receipt)
        pending.pop(pending_id, None)

        if result["duplicate"]:
            await query.edit_message_text("⚠️ Ce ticket existe déjà dans Notion.")
            return

        remaining = float(result["remaining"])
        warning = "🔴 Budget dépassé." if remaining < 0 else "🟢 Budget respecté."
        await query.edit_message_text(
            receipt_summary(receipt)
            + f"\n\n✅ Ajouté dans Notion.\n"
            + f"Dépensé cette semaine : {float(result['spent']):.2f} €\n"
            + f"Reste : {remaining:.2f} €\n{warning}"
        )
    except Exception as exc:
        LOGGER.exception("Ajout Notion impossible")
        await query.edit_message_text(f"❌ Ajout Notion impossible : {exc}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Erreur Telegram", exc_info=context.error)


def main() -> None:
    application: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(ok|cancel):"))
    application.add_error_handler(error_handler)

    port = os.getenv("PORT", "").strip()
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if port and hostname:
        webhook_path = os.getenv("WEBHOOK_PATH", "telegram").strip("/") or "telegram"
        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip() or hashlib.sha256(
            TELEGRAM_BOT_TOKEN.encode("utf-8")
        ).hexdigest()
        webhook_url = f"https://{hostname}/{webhook_path}"
        LOGGER.info(
            "Bot démarré en mode webhook sur Render. AUTO_CONFIRM=%s",
            AUTO_CONFIRM,
        )
        application.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=webhook_path,
            webhook_url=webhook_url,
            secret_token=webhook_secret,
            bootstrap_retries=-1,
            allowed_updates=Update.ALL_TYPES,
        )
        return

    LOGGER.info("Bot démarré en mode polling local. AUTO_CONFIRM=%s", AUTO_CONFIRM)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
