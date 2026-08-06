# Telegram receipt → Notion

The bot reads French, Spanish, and English receipts, lets the user verify the
detected lines, adds purchased items to Notion, and reports the remaining amount
from a weekly budget (20 € by default).

## Notion structure

`Dépenses & Courses` and `Semaines` are connected through the two-way relation
`Semaine` / `Dépenses`.

- `Semaines.Dépensé` is a rollup: sum of `Dépenses.Prix réel`.
- `Semaines.Reste` is a formula: `Budget - Dépensé`.
- A week is identified by its `Début` date (Monday), not by its display title.

This means adding, editing, or deleting a linked expense automatically updates
the weekly spent and remaining amounts inside Notion.

## Receipt recognition

RapidOCR with PP-OCRv6 is the primary reader. It uses a memory-efficient text
detector and multilingual recognizer, then rebuilds product rows from the text
box coordinates. Tesseract remains available as an automatic fallback. The
parser understands locale-aware prices (`0.93`, `0,93`, `1.234,56`, and
`1,234.56`), uses a product line's final amount instead of its weight or unit
price, and checks that product lines reconcile with the receipt total.

Telegram compresses regular photos. For difficult or small-print receipts, send
the image as a file/document so the bot receives the original resolution.

Install the Tesseract language packs for Spanish, French, and English. On macOS:

```sh
brew install tesseract tesseract-lang
```

## Run

Create `.env` with the Telegram and Notion credentials, then:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

Optional settings:

```dotenv
WEEKLY_BUDGET=20
TIMEZONE=Europe/Madrid
OCR_LANGUAGES=spa+fra+eng
RAPIDOCR_ENABLED=true
AUTO_CONFIRM=false
```

Keep `AUTO_CONFIRM=false` so uncertain OCR output is shown with warnings before
anything is written to Notion.

## Deploy on Render

The repository includes a `Dockerfile` and `render.yaml`. The Render Blueprint
creates a free web service and asks for the five private values that are marked
with `sync: false`. Secrets stay in Render and are never committed to GitHub.

On Render, the bot automatically switches from local polling to a secured
Telegram webhook. Render provides HTTPS and the public hostname; the application
listens on Render's `PORT`. The free service can sleep after 15 minutes without
incoming traffic, and the next Telegram webhook wakes it again.
