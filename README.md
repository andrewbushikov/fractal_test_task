# AI Inbox Classifier

Сервіс для автоматичної класифікації вхідних запитів від внутрішніх команд через LLM. Читає `input_requests.csv`, класифікує кожен запит через Groq, валідує вивід і генерує `output.json`, `report.md`, пише результати в Google Sheets та надсилає дайджест у Telegram-канал.

---

## Архітектура

```
input_requests.csv
       │
       ▼
  read_csv()
       │
       ▼
classify_all()  ── послідовно, з паузою REQUEST_DELAY між запитами
       │
  classify_one()
   ├── Groq API (llama-3.3-70b-versatile)
   ├── extract_json()       — стрипає ```json``` фенси
   ├── LLMRawOutput         — Pydantic валідація (шар 1: enum-значення)
   ├── ClassifiedRequest    — Pydantic валідація (шар 2: coercions)
   └── fallback             — safe запис при вичерпанні retry
       │
  ┌────┴──────────────────┐
  │                       │
output.json           report.md
                          │
                  write_google_sheet()
                          │
                   send_telegram()
```

**Два шари валідації LLM-виводу:**
- `LLMRawOutput` — перевіряє що `category` і `priority` з дозволеного enum, ловить невалідний JSON
- `ClassifiedRequest` — фінальна модель з coercion (рядок → список для `requested_actions`, lowercase для `priority`)

---

## Як запустити

### 1. Вимоги

- Python 3.11+
- Ключ Groq API

### 2. Встановлення залежностей

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Конфігурація

```bash
cp .env.example .env
# Відкрийте .env і заповніть змінні
```

### 4. Запуск

```bash
python3 classifier.py
```

Результат — три файли (якщо налаштовані всі інтеграції):
- `output.json` — повна структурована класифікація
- `report.md` — агрегований звіт
- Google Sheet — кожен запит окремим рядком
- Telegram-канал — короткий дайджест

---

## Змінні середовища

| Змінна | За замовчуванням | Обов'язково | Опис |
|--------|------------------|-------------|------|
| `GROQ_API_KEY` | — | ✅ | API ключ Groq ([console.groq.com](https://console.groq.com)) |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | | Модель Groq |
| `INPUT_CSV` | `input_requests.csv` | | Шлях до вхідного файлу |
| `OUTPUT_JSON` | `output.json` | | Шлях до вихідного JSON |
| `OUTPUT_REPORT` | `report.md` | | Шлях до звіту |
| `MAX_CONCURRENT` | `2` | | Паралельність (краще не чіпати на free tier) |
| `REQUEST_DELAY` | `5.0` | | Пауза між запитами в секундах |
| `TELEGRAM_BOT_TOKEN` | — | | Token бота від @BotFather |
| `TELEGRAM_CHANNEL_ID` | — | | ID каналу, наприклад `-100xxxxxxxxxx` |
| `SPREADSHEET_ID` | — | | ID Google Sheets таблиці |
| `GOOGLE_SHEET_NAME` | `Sheet1` | | Назва листа |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | `service_account.json` | | Шлях до JSON service account |

---

## Схема класифікованого запиту

```json
{
  "request_id": "REQ-001",
  "channel": "slack",
  "timestamp": "2024-01-15 09:12:34",
  "raw_text": "...",
  "category": "інтеграція",
  "target_department": "Продажі",
  "priority": "high",
  "short_summary": "Щоденна синхронізація CRM → Google Sheets.",
  "requested_actions": [
    "Налаштувати scheduled sync CRM → Google Sheets о 9:00"
  ],
  "needs_clarification": false,
  "sentiment": "urgent",
  "estimated_complexity": "medium",
  "clarification_question": null,
  "llm_confidence": "high"
}
```

**Поля понад мінімум завдання:**

| Поле | Навіщо |
|------|--------|
| `sentiment` | Відрізняє «горить» від «коли зможете» — допомагає з пріоритизацією |
| `estimated_complexity` | Швидка оцінка для планування спринту |
| `clarification_question` | Якщо `needs_clarification=true` — готове питання для відправки назад |
| `llm_confidence` | Сигнал для ручного рев'ю: `low` → перевірити очима |

---

## Налаштування інтеграцій

### Telegram

1. Створи бота через [@BotFather](https://t.me/BotFather) → `/newbot` → отримай токен
2. Додай бота адміністратором каналу з правом публікації повідомлень
3. Chat ID каналу можна отримати через [@userinfobot](https://t.me/userinfobot) або з URL

```env
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx
```

Якщо змінні не задані — крок пропускається з warning, запуск не ламається.

### Google Sheets

1. Google Cloud Console → IAM → Service Accounts → Create
2. Keys → Add Key → JSON → завантаж файл → поклади в корінь проекту як `service_account.json`
3. Відкрий таблицю → Share → додай email service account з правами **редактора**
4. `SPREADSHEET_ID` береться з URL таблиці:
   `docs.google.com/spreadsheets/d/**ЦЕ_І_Є_ID**/edit`

```env
SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
GOOGLE_SHEET_NAME=Sheet1
GOOGLE_SERVICE_ACCOUNT_JSON=service_account.json
```

Якщо лист порожній — заголовки пишуться автоматично. Дані дописуються (`append_rows`), не перезаписують існуючі.

Колонки: `ID, Канал, Час, Категорія, Відділ, Пріоритет, Суть, Дії, Потребує уточнення, Питання, Складність, Тональність, Впевненість, Текст запиту`

Якщо змінні не задані — крок пропускається з warning.

---

## Docker

```bash
docker build -t ai-inbox-classifier .
docker run --env-file .env \
  -v $(pwd)/input_requests.csv:/app/input_requests.csv \
  -v $(pwd)/service_account.json:/app/service_account.json \
  -v $(pwd)/output:/app/output \
  ai-inbox-classifier
```

---

## Де рішення ламається / обмеження

### Rate limits Groq

Free tier: **30 RPM / 6000 TPM** на модель. При одночасних запитах легко вичерпується.

**Як закрито:** послідовна обробка з `REQUEST_DELAY=5.0` між запитами. При `429` читаємо заголовок `retry-after` з відповіді Groq і чекаємо рівно стільки скільки треба (`wait = int(e.response.headers.get("retry-after", 60)) + 1`).

**Що не закрито:** якщо запустити декілька інстансів одночасно — знову впремося в ліміт.

### Невалідний вивід LLM

**Що може піти не так:** модель повертає Markdown-блок замість голого JSON, використовує категорію не з enum, повертає рядок замість списку.

**Як закрито:**
- `extract_json()` стрипає ` ```json ``` ` маркери
- `LLMRawOutput` валідує enum-значення
- `coerce_actions` перетворює рядок на список
- 3 retry при помилках парсингу/валідації
- Safe fallback із `llm_confidence=low` і `needs_clarification=True` якщо retry вичерпані

### Великий обсяг

При 1000+ запитах з `REQUEST_DELAY=5` час виконання ~1.5 години. Рішення: перейти на платний Groq tier і збільшити `MAX_CONCURRENT`.

Зараз всі результати в пам'яті до запису в файл — при дуже великому CSV (~10k рядків) варто стрімити результати одразу.

### Недетермінізм

Та сама фраза може отримати різну категорію при різних запусках. Закрито частково: `temperature=0.1` зменшує варіативність, `llm_confidence` позначає невпевнені класифікації для ручного рев'ю.

---

## Структура проекту

```
ai-inbox-classifier/
├── classifier.py          # Головний сервіс
├── models.py              # Pydantic-схеми (два шари валідації)
├── input_requests.csv     # Вхідні дані
├── output.json            # Результат (генерується)
├── report.md              # Звіт (генерується)
├── service_account.json   # Google SA ключ (не комітити!)
├── requirements.txt
├── Dockerfile
├── .env.example
└── .gitignore
```

---

## Що зробив би далі

1. **Groq JSON mode** — передавати `response_format={"type": "json_object"}` щоб модель гарантовано повертала JSON і прибрати retry-логіку для невалідного формату
2. **Кешування** — SQLite/Redis щоб не перекласифіковувати однакові запити при повторному запуску
3. **Streaming до БД** — писати результати рядок-за-рядком у PostgreSQL замість bulk запису в кінці
4. **CI тести** — pytest зі snapshot-тестами на mock Groq responses
5. **Дедуплікація** — REQ-013 у тестових даних є дублем REQ-001, варто це детектувати