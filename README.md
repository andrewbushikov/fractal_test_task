# AI Inbox Classifier

Сервіс для автоматичної класифікації вхідних запитів від внутрішніх команд
через LLM. Читає `input_requests.csv`, класифікує кожен запит, валідує вивід
і генерує `output.json` + `report.md`.

---

## Архітектура

```
input_requests.csv
       │
       ▼
  read_csv()
       │
       ▼
classify_all()  ←── asyncio.gather + Semaphore(5)
       │
   ┌───┴──────────────┐
   │  classify_one()  │  × N запитів паралельно
   │   Groq API  │
   │   → extract_json │
   │   → LLMRawOutput │  Pydantic validation (шар 1)
   │   → ClassifiedRequest  (шар 2 + coercions)
   └───┬──────────────┘
       │
  ┌────┴─────┐
  │          │
output.json  report.md
```

**Два шари валідації:**
1. `LLMRawOutput` — перевіряє що категорія і пріоритет з дозволеного enum
2. `ClassifiedRequest` — фінальна модель з coercion (наприклад, рядок → список для `requested_actions`)

---

## Як запустити

### 1. Вимоги

- Python 3.11+
- Ключ Groq API

### 2. Встановлення залежностей

```bash
pip install -r requirements.txt
```

### 3. Конфігурація

```bash
cp .env.example .env
# Відкрийте .env і вставте свій GROQ_API_KEY
```

**Змінні середовища:**

| Змінна | За замовчуванням | Опис |
|--------|------------------|------|
| `GROQ_API_KEY` | — | **Обов'язково.** API ключ Groq |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Модель Claude |
| `INPUT_CSV` | `input_requests.csv` | Шлях до вхідного файлу |
| `OUTPUT_JSON` | `output.json` | Шлях до вихідного JSON |
| `OUTPUT_REPORT` | `report.md` | Шлях до звіту |
| `MAX_CONCURRENT` | `5` | Кількість паралельних запитів до API |

### 4. Запуск

```bash
python classifier.py
```

Результат — два файли:
- `output.json` — повна структурована класифікація
- `report.md` — агрегований звіт

### 5. Docker

```bash
docker build -t ai-inbox-classifier .
docker run --env-file .env -v $(pwd):/app ai-inbox-classifier
```

---

## Схема класифікованого запиту

```json
{
  "request_id": "1",
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

**Чому додав поля понад мінімум:**

| Поле | Причина |
|------|---------|
| `sentiment` | Допомагає відрізнити «горить» від «коли зможете» — корисно для приоритизації |
| `estimated_complexity` | Швидка оцінка для планування спринту |
| `clarification_question` | Якщо `needs_clarification=true` — готове питання, яке можна відразу надіслати назад |
| `llm_confidence` | Сигнал для ручного рев'ю: low → перевірити очима |

---

## Де рішення ламається / обмеження

### Невалідний вивід LLM

**Що може піти не так:**
- LLM повертає Markdown-блок замість голого JSON
- Використовує категорію не з дозволеного списку
- Повертає рядок замість списку для `requested_actions`
- Поле `priority` у верхньому регістрі (`"HIGH"`)

**Як закрито:**
- `extract_json()` стрипає ` ```json ``` ` маркери
- Pydantic валідатори нормалізують пріоритет до lowercase
- `coerce_actions` перетворює рядок на список
- 3 retry з логуванням на кожну спробу
- Якщо всі retry вичерпані — safe fallback запис з `llm_confidence=low` і `needs_clarification=True`

**Що не закрито:** LLM може галюцинувати поза схемою (наприклад, вигадати нову категорію). Це ловиться валідацією, але retry не обов'язково виправить — модель може наполягати. Рішення: більш жорсткий system prompt + Groq JSON mode із примусовою JSON-схемою.

### Великий обсяг

- `MAX_CONCURRENT=5` — захист від rate limit. Можна збільшити для Tier 2+ акаунтів.
- При 1000+ запитів варто розбити на батчі по ~100 і зберігати проміжні результати, щоб не втратити прогрес при падінні.
- Зараз всі результати в пам'яті до запису — при дуже великому CSV (~10k рядків) краще стрімити в JSON одразу.

### Недетермінізм

Та сама фраза може отримати різну категорію при різних runs. Це нормально для LLM, але:
- Логуємо `llm_confidence` щоб позначити невпевнені класифікації
- Для продакшн-використання: кешувати результати по `hash(raw_text)` і не перекласифіковувати незмінені запити

### Вартість токенів

При `llama-3.3-70b-versatile` і середньому запиті ~200 токенів:
- System prompt ≈ 450 токенів (фіксовано)
- Вхід + вихід ≈ 700 токенів на запит
- 1000 запитів ≈ 700k токенів ≈ ~$2-3

Для зниження вартості: `llama-3.1-8b-instant` у ~10x дешевший і добре справляється з простими класифікаційними задачами.

---

## Що зробив би далі

1. **Groq JSON mode** — замість парсингу тексту передати JSON-схему напряму, прибрати retry-логіку для неправильного формату
2. **Кешування** — Redis/SQLite щоб не перекласифіковувати однакові запити
3. **Стрімінг у БД** — писати результати у PostgreSQL рядок-за-рядком замість bulk запису
4. **Telegram digest** — відправляти `report.md` у Telegram-бот після завершення
5. **Google Sheets інтеграція** — `gspread` + service account для запису результатів
6. **CI/CD тест** — pytest зі snapshot-тестами на mock LLM responses
7. **Веб-інтерфейс** — FastAPI endpoint для real-time класифікації одного запиту

---

## Структура проекту

```
ai-inbox-classifier/
├── classifier.py          # Головний сервіс
├── models.py              # Pydantic-схеми
├── input_requests.csv     # Вхідні дані
├── output.json            # Результат (генерується)
├── report.md              # Звіт (генерується)
├── requirements.txt
├── Dockerfile
├── .env.example
└── .gitignore
```
