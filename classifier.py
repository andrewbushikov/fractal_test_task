"""
AI Inbox Classifier — core service.

Reads input_requests.csv, classifies each request via Groq,
validates output with Pydantic, and writes output.json + report.md.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import gspread
from google.oauth2.service_account import Credentials
from groq import AsyncGroq, RateLimitError, APIError
from dotenv import load_dotenv
from pydantic import ValidationError

from models import Category, ClassifiedRequest, LLMRawOutput, Priority

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))  # Groq free: 30 RPM / 6k TPM
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "2.0"))  # seconds between request starts
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
INPUT_CSV = Path(os.getenv("INPUT_CSV", "input_requests.csv"))
OUTPUT_JSON = Path(os.getenv("OUTPUT_JSON", "output.json"))
OUTPUT_REPORT = Path(os.getenv("OUTPUT_REPORT", "report.md"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

SYSTEM_PROMPT = """Ти — класифікатор вхідних запитів для AI-юніту компанії.
Твоє завдання: проаналізувати текст запиту і повернути ВИКЛЮЧНО валідний JSON (без будь-якого тексту до або після).

Доступні категорії:
- "автоматизація" — автоматизація бізнес-процесів, scheduled jobs, бот-флоу
- "інтеграція" — підключення систем, API, Zapier/Make, webhooks
- "звіт/аналітика" — побудова звітів, дашбордів, аналіз даних
- "баг/підтримка" — щось зламалось, не працює, технічна проблема
- "питання/консультація" — запит на консультацію, оцінку можливостей
- "поза скоупом" — запит не стосується роботи AI-юніту (дизайн, HR-рекрутинг, фінанси тощо)

Пріоритет визначай за тоном і змістом:
- "high" — явна терміновість, виробничий збій, клієнт вже постраждав
- "medium" — є дедлайн або бізнес-вплив, але не критично
- "low" — нема дедлайну, загальне питання

Поверни JSON такого формату (всі ключі обов'язкові крім зазначених як опціональні):
{
  "category": "<одне з дозволених значень>",
  "target_department": "<назва відділу або null>",
  "priority": "low" | "medium" | "high",
  "short_summary": "<суть одним реченням>",
  "requested_actions": ["<дія 1>", "<дія 2>"],
  "needs_clarification": true | false,
  "sentiment": "neutral" | "frustrated" | "urgent" | "polite",
  "estimated_complexity": "simple" | "medium" | "complex",
  "clarification_question": "<питання для уточнення або null>",
  "llm_confidence": "low" | "medium" | "high"
}

Правила:
- requested_actions — конкретні дії (дієслово + об'єкт), порожній список якщо нічого конкретного
- needs_clarification=true якщо запит занадто розмитий для виконання
- clarification_question — тільки якщо needs_clarification=true, інакше null
- Відповідай ТІЛЬКИ JSON, без ```json``` маркерів та пояснень"""


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response, stripping markdown fences if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read CSV and return list of row dicts."""
    if not path.exists():
        log.error(f"Input file not found: {path}")
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    log.info(f"Read {len(rows)} rows from {path}")
    return rows


# ──────────────────────────────────────────────
# LLM Classification
# ──────────────────────────────────────────────

async def classify_one(
    client: AsyncGroq,
    row: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> ClassifiedRequest:
    """Classify a single request row. Falls back to error placeholder on failure."""
    async with semaphore:
        request_id = row.get("id", "?")
        raw_text = row.get("raw_text", "").strip()
        log.info(f"[{request_id}] Classifying: {raw_text[:60]}…")

        for attempt in range(1, 4):  # up to 3 retries
            try:
                response = await client.chat.completions.create(
                    model=MODEL,
                    max_tokens=1024,
                    temperature=0.1,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": raw_text or "(порожній запит)"},
                    ],
                )
                raw_content = response.choices[0].message.content
                log.debug(f"[{request_id}] Raw LLM response: {raw_content[:200]}")

                parsed_dict = extract_json(raw_content)
                llm_output = LLMRawOutput(**parsed_dict)

                return ClassifiedRequest(
                    request_id=request_id,
                    channel=row.get("channel", ""),
                    timestamp=row.get("timestamp", ""),
                    raw_text=raw_text,
                    **llm_output.model_dump(),
                )

            except json.JSONDecodeError as e:
                log.warning(f"[{request_id}] Attempt {attempt}: JSON parse error — {e}")
            except ValidationError as e:
                log.warning(f"[{request_id}] Attempt {attempt}: Validation error — {e}")
            except RateLimitError as e:
                # Groq returns retry-after header with exact reset time
                wait = 60  # fallback if header missing
                try:
                    wait = int(e.response.headers.get("retry-after", 60)) + 1
                except Exception:
                    pass
                log.warning(f"[{request_id}] Rate limit hit, waiting {wait}s…")
                await asyncio.sleep(wait)
            except APIError as e:
                log.error(f"[{request_id}] API error: {e}")
                break

        # All retries exhausted — return a safe fallback
        log.error(f"[{request_id}] Classification failed after retries. Using fallback.")
        return ClassifiedRequest(
            request_id=request_id,
            channel=row.get("channel", ""),
            timestamp=row.get("timestamp", ""),
            raw_text=raw_text,
            category=Category.QUESTION_CONSULTING,
            target_department=None,
            priority=Priority.LOW,
            short_summary="[ПОМИЛКА КЛАСИФІКАЦІЇ] Не вдалося обробити запит.",
            requested_actions=[],
            needs_clarification=True,
            sentiment="neutral",
            estimated_complexity="simple",
            clarification_question="Будь ласка, повторіть запит детальніше.",
            llm_confidence="low",
        )


async def classify_all(rows: list[dict[str, str]]) -> list[ClassifiedRequest]:
    """Classify all rows with staggered starts to respect Groq rate limits."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log.error("GROQ_API_KEY not set. Add it to .env or environment.")
        sys.exit(1)

    client = AsyncGroq(api_key=api_key)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def staggered(i: int, row: dict[str, str]) -> ClassifiedRequest:
        await asyncio.sleep(i * REQUEST_DELAY)
        return await classify_one(client, row, semaphore)

    tasks = [staggered(i, row) for i, row in enumerate(rows)]
    results = await asyncio.gather(*tasks)
    return list(results)


# ──────────────────────────────────────────────
# Output: JSON
# ──────────────────────────────────────────────

def write_json(results: list[ClassifiedRequest], path: Path) -> None:
    data = [r.model_dump() for r in results]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Wrote {len(results)} records to {path}")


# ──────────────────────────────────────────────
# Output: Markdown report
# ──────────────────────────────────────────────

def write_report(results: list[ClassifiedRequest], path: Path) -> None:
    total = len(results)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    by_category: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_department: dict[str, int] = {}
    needs_clarification: list[ClassifiedRequest] = []

    for r in results:
        by_category[r.category.value] = by_category.get(r.category.value, 0) + 1
        by_priority[r.priority.value] = by_priority.get(r.priority.value, 0) + 1
        dept = r.target_department or "невідомо"
        by_department[dept] = by_department.get(dept, 0) + 1
        if r.needs_clarification:
            needs_clarification.append(r)

    lines: list[str] = [
        "# AI Inbox — Звіт класифікації",
        "",
        f"**Дата генерації:** {now}  ",
        f"**Всього запитів:** {total}",
        "",
        "---",
        "",
        "## По категоріях",
        "",
        "| Категорія | Кількість |",
        "|-----------|-----------|",
    ]
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {count} |")

    lines += [
        "",
        "## По пріоритету",
        "",
        "| Пріоритет | Кількість |",
        "|-----------|-----------|",
    ]
    for prio in ["high", "medium", "low"]:
        lines.append(f"| {prio} | {by_priority.get(prio, 0)} |")

    lines += [
        "",
        "## По відділах",
        "",
        "| Відділ | Кількість |",
        "|--------|-----------|",
    ]
    for dept, count in sorted(by_department.items(), key=lambda x: -x[1]):
        lines.append(f"| {dept} | {count} |")

    lines += [
        "",
        "---",
        "",
        f"## Потребують уточнення ({len(needs_clarification)} з {total})",
        "",
    ]
    if needs_clarification:
        for r in needs_clarification:
            lines.append(f"### Запит #{r.request_id} [{r.channel}]")
            lines.append(f"**Текст:** {r.raw_text[:120]}{'…' if len(r.raw_text) > 120 else ''}")
            lines.append(f"**Суть:** {r.short_summary}")
            if r.clarification_question:
                lines.append(f"**Питання для уточнення:** {r.clarification_question}")
            lines.append("")
    else:
        lines.append("_Усі запити достатньо чіткі._")

    lines += [
        "",
        "---",
        "",
        "## Всі запити (короткий список)",
        "",
        "| # | Канал | Категорія | Пріоритет | Відділ | Суть |",
        "|---|-------|-----------|-----------|--------|------|",
    ]
    for r in results:
        dept = r.target_department or "—"
        summary = r.short_summary[:60] + "…" if len(r.short_summary) > 60 else r.short_summary
        lines.append(
            f"| {r.request_id} | {r.channel} | {r.category.value} "
            f"| {r.priority.value} | {dept} | {summary} |"
        )

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Report written to {path}")



# ──────────────────────────────────────────────
# Google Sheets export
# ──────────────────────────────────────────────

SHEET_HEADERS = [
    "ID", "Канал", "Час", "Категорія", "Відділ", "Пріоритет",
    "Суть", "Дії", "Потребує уточнення", "Питання", "Складність",
    "Тональність", "Впевненість", "Текст запиту",
]


def write_google_sheet(results: list[ClassifiedRequest]) -> None:
    """Append classified results to an existing Google Sheet."""
    if not SPREADSHEET_ID:
        log.warning("SPREADSHEET_ID not set — skipping Google Sheets export.")
        return

    sa_path = Path(SERVICE_ACCOUNT_JSON)
    if not sa_path.exists():
        log.error(f"Service account file not found: {sa_path}")
        return

    try:
        creds = Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(SHEET_NAME)

        # Write headers if sheet is empty
        if ws.row_count == 0 or not ws.row_values(1):
            ws.append_row(SHEET_HEADERS, value_input_option="RAW")

        rows = []
        for r in results:
            rows.append([
                r.request_id,
                r.channel,
                r.timestamp,
                r.category.value,
                r.target_department or "",
                r.priority.value,
                r.short_summary,
                "; ".join(r.requested_actions),
                "Так" if r.needs_clarification else "Ні",
                r.clarification_question or "",
                r.estimated_complexity or "",
                r.sentiment or "",
                r.llm_confidence or "",
                r.raw_text,
            ])

        ws.append_rows(rows, value_input_option="RAW")
        log.info(f"Written {len(rows)} rows to Google Sheet '{SHEET_NAME}'.")

    except gspread.exceptions.SpreadsheetNotFound:
        log.error(f"Spreadsheet not found: {SPREADSHEET_ID}. Check ID and sharing settings.")
    except gspread.exceptions.WorksheetNotFound:
        log.error(f"Worksheet '{SHEET_NAME}' not found. Check GOOGLE_SHEET_NAME.")
    except Exception as e:
        log.error(f"Google Sheets error: {e}")


# ──────────────────────────────────────────────
# Telegram digest
# ──────────────────────────────────────────────

def build_digest(results: list[ClassifiedRequest]) -> str:
    """Build a short digest message for Telegram."""
    total = len(results)
    by_cat: dict[str, int] = {}
    by_prio: dict[str, int] = {}
    needs_clarification = []

    for r in results:
        by_cat[r.category.value] = by_cat.get(r.category.value, 0) + 1
        by_prio[r.priority.value] = by_prio.get(r.priority.value, 0) + 1
        if r.needs_clarification:
            needs_clarification.append(r)

    prio_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}

    lines = [
        "📬 *AI Inbox — дайджест*",
        f"Оброблено запитів: *{total}*",
        "",
        "*По категоріях:*",
    ]
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  • {cat}: {count}")

    lines += ["", "*По пріоритету:*"]
    for prio in ["high", "medium", "low"]:
        count = by_prio.get(prio, 0)
        lines.append(f"  {prio_icons[prio]} {prio}: {count}")

    if needs_clarification:
        lines += ["", f"*Потребують уточнення ({len(needs_clarification)}):*"]
        for r in needs_clarification:
            lines.append(f"  ❓ {r.request_id} — {r.short_summary[:60]}")

    return "\n".join(lines)


async def send_telegram(text: str) -> None:
    """Send message to Telegram channel via Bot API."""
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID not set — skipping Telegram.")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                log.info("Telegram digest sent successfully.")
            else:
                body = await resp.text()
                log.error(f"Telegram error {resp.status}: {body}")

# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

async def main() -> None:
    log.info("=== AI Inbox Classifier starting ===")
    rows = read_csv(INPUT_CSV)
    results = await classify_all(rows)
    write_json(results, OUTPUT_JSON)
    write_report(results, OUTPUT_REPORT)
    write_google_sheet(results)
    digest = build_digest(results)
    await send_telegram(digest)
    log.info("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())