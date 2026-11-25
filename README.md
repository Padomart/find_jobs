# Поиск работы: Go/Python backend, Junior+/Middle

Два независимых инструмента и общий код:

| Папка | Что делает | Запуск (из корня папки) |
|---|---|---|
| `en/` | Ищет небольшие зарубежные компании на Greenhouse / Lever / Ashby и готовит списки бордов для автоматизаций JobSync (по 25 на провайдера) | `python3 en/find_boards.py --hn-thread 50338728 --remote-only` |
| `ru/` | Ищет вакансии на российском рынке (Хабр Карьера, SuperJob, «Работа России», Telegram, Яндекс, VK, МТС) | `python3 ru/find_ru_jobs.py` |
| `common/` | Общий код: HTTP с ретраями (`httpclient.py`), клиент JobSync MCP (`jobsync_push.py`), фильтры (`filters.py`), файлы состояния (`state.py`) | `python3 common/jobsync_push.py --dry-run` |

Нужен только Python 3.9+ (стандартная библиотека). Необязательные пакеты: `certifi` (лечит ошибку
SSL на macOS) и `PyYAML` (ru-скрипт без него читает конфиг встроенным разборщиком).

## Структура

```
find_jobs/
├── README.md, .gitignore, .env.example
├── common/            общий код (пакет: __init__.py, httpclient.py, jobsync_push.py, filters.py, state.py)
├── en/                find_boards.py, blacklist.txt, README.md, out/ (результаты)
├── ru/                find_ru_jobs.py, config_ru.yaml, README.md, out/ (результаты)
└── state/             seen_boards.json, seen_ru.json, jobsync_ledger.json, cache/boards/
```

Каждый инструмент пишет только в свой `out/` и свой файл в `state/`. Учёт лимита JobSync
(`state/jobsync_ledger.json`) общий намеренно: лимит 60 вызовов в час один на все токены.

## Как устроены импорты

Скрипты запускаются как обычные файлы: `python3 en/find_boards.py`, `python3 ru/find_ru_jobs.py`,
`python3 common/jobsync_push.py`. Каждая точка входа сама добавляет корень проекта в `sys.path`
и импортирует `from common import ...`, поэтому запускать можно из любой папки, а пути по умолчанию
(`out/`, `state/`, конфиг, чёрный список) считаются от расположения скрипта, а не от текущей папки.

## Переменные окружения

Секреты задаются только переменными окружения, в коде и конфигах их нет. Шаблон — `.env.example`
(скопируйте в `.env` и заполните; `.env` в `.gitignore`). Скрипты сами `.env` не читают — загрузите его в shell:

```bash
set -a; source .env; set +a
```

| Переменная | Кто использует | Зачем |
|---|---|---|
| `JOBSYNC_URL`, `JOBSYNC_TOKEN` | `common/jobsync_push.py`, `ru --push-jobsync` | отправка вакансий в JobSync через MCP |
| `DEEPSEEK_API_KEY` | `en --ai`, `ru --ai` | AI-оценка вакансий (`common/ai_score.py`) |
| `SUPERJOB_KEY` | `ru` (источник superjob) | ключ приложения SuperJob |

## Фильтры по умолчанию

- Максимум требуемого опыта: 5 лет (`en --max-years`, `ru/config_ru.yaml: max_years_required`).
- en: remote подходит, только если worldwide/anywhere/APAC/Asia/Вьетнам или регион не указан;
  «remote только для одной страны/региона» (США, Индия, Литва, Европа…) отсеивается (`--remote-regions`).
  Переезд — при спонсорстве визы/релокации. Вакансии только в России — никогда.
- ru: удалёнка, в том числе «по России».

## AI-оценка вакансий

Общий модуль `common/ai_score.py`: DeepSeek (OpenAI-совместимый API) оценивает вакансию под резюме
от 0 до 10, с уровнем, «Go основной?», короткой причиной и красными флагами. Работает в обоих поисках:

```bash
python3 en/find_boards.py --hn --hn-months 3 --sources all --remote-only --watchlist --ai
```

```bash
python3 ru/find_ru_jobs.py --ai
```

- Резюме — `resume.txt` в корне (в `.gitignore`, PDF-резюме `resume*.pdf` тоже). 5–15 строк: стек, опыт,
  уровень, страна, пожелания.
- Платные запросы — только по прошедшим фильтры вакансиям, не больше `--ai-limit` (40) за запуск;
  оценки кэшируются в `state/ai_scores.json`, повтор бесплатный.
- en сортирует борды и вакансии без борда по AI-оценке, ru — вакансии.
- В JobSync оценка попадает в шапку описания; `common/jobsync_push.py --min-ai 6` отправляет только
  вакансии с оценкой от 6.

JobSync оценивает сам и независимо от нас: автоматизации бордов (Greenhouse/Lever/Ashby) прогоняют
каждую новую вакансию через AI-матч с резюме из профиля и сохраняют только те, что выше порога
автоматизации; для вакансий, добавленных через MCP, матч автоматически не запускается — кнопка
Match в карточке (или AI-агент, подключённый к MCP).

## JobSync: описание вакансии

`common/jobsync_push.py` шлёт описание в поле `jobDescription` как Markdown (JobSync сам рендерит его
в HTML; сырой HTML он экранирует, поэтому HTML слать нельзя). Сверху — служебные строки: источник и
дата, ссылка на оригинал, где/remote, стек, опыт, ограничения. Кракозябры RemoteOK («Weâ€™re») чинятся,
`--max-desc` режет по границе абзаца и не меньше 300 символов. Файл может быть и `ru/out/jobs_ru.json`.

Обновить уже отправленные вакансии (по URL, через upsert, без дублей; 1 вызов лимита на вакансию):

```bash
python3 common/jobsync_push.py --resend --dry-run
```

```bash
python3 common/jobsync_push.py --resend
```

Внимание: upsert заменяет теги вакансии целиком на теги из скрипта.

Подробности и флаги — в `en/README.md` и `ru/README.md`.
