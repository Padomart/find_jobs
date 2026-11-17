# boards — небольшие компании на Greenhouse / Lever / Ashby

Ищет небольшие компании с вакансиями Go/Python backend уровня Junior/Middle (remote из Вьетнама
или с релокацией) и готовит списки бордов для автоматизаций JobSync.

## Примеры (из корня папки)

```bash
python3 boards/find_boards.py --hn-thread 50338728 --remote-only
```

```bash
python3 boards/find_boards.py --hn --hn-months 3 --sources all --remote-only --watchlist
```

```bash
python3 boards/find_boards.py --seed companies.txt --max-jobs 40
```

Полный список флагов: `python3 boards/find_boards.py --help`.

## Основные флаги

| Флаг | По умолчанию | Что делает |
|---|---|---|
| `--hn`, `--hn-months N`, `--hn-thread ID` | — | кандидаты из «Ask HN: Who is hiring?» |
| `--sources` | — | `remoteok,remotive,himalayas,arbeitnow,jobicy` или `all` |
| `--seed FILE` | — | названия, домены, токены или ссылки на борды |
| `--remote-only` | выкл. | только remote из разрешённых регионов, либо релокация/виза |
| `--remote-regions` | `global,apac` | какие ограничения региона у remote подходят (ещё: `eu`, `us`, `ca`, `americas`, `other`) |
| `--block-regions` | `us,ca,ru` | офис там — только при спонсорстве визы |
| `--max-years N` | 5 | отсеивать требования опыта больше N лет |
| `--stale-days N` | 30 | помечать «старое», если объявлению больше N дней |
| `--ai`, `--ai-limit N` | выкл., 40 | AI-оценка подходящих вакансий под `resume.txt` (DeepSeek), сортировка по ней |
| `--max-jobs N` | 50 | компания «маленькая», если вакансий не больше |
| `--watchlist` | выкл. | добавлять небольшие компании с Go в стеке без текущих подходящих вакансий |
| `--blacklist` | `boards/blacklist.txt` | крупные/неинтересные компании |
| `--out-dir` | `boards/out` | куда писать результаты |
| `--state` | `state/seen_boards.json` | состояние между запусками |
| `--cache-dir` | `state/cache/boards` | кэш ответов HN и источников |

## Что появляется в `boards/out/`

| Файл | Что внутри |
|---|---|
| `boards_greenhouse.txt`, `boards_lever.txt`, `boards_ashby.txt` | до 25 ссылок: одна автоматизация JobSync на файл |
| `report.md` | статистика провайдеров за запуск, таблица бордов, подходящие вакансии со ссылками и датой публикации (⚠️ старое, ⚠️ текст неполный, репост) |
| `boards.json` | все найденные борды с деталями |
| `jobs_noats.json` | подходящие вакансии у компаний без борда — для `common/jobsync_push.py` |

Повторный запуск не проверяет заново токены без борда (21 день, `--recheck-days`) и помечает новые компании.

## Вакансии без ATS → JobSync

```bash
python3 common/jobsync_push.py --dry-run
```

```bash
set -a; source .env; set +a
python3 common/jobsync_push.py --limit 20
```

Батчи по 10, не больше 50 вызовов в час (`--budget`), общий учёт `state/jobsync_ledger.json`, повторно
отправленное не дублируется (upsert + список отправленных ссылок).

## Фильтр регионов для remote

Remote подходит, если регион worldwide/APAC/Вьетнам или не указан. Но вакансия отсеивается, если в
заголовке или тексте есть ограничение: LATAM / Latin America, «EU only», «US only», «remote (US)»,
«PST/EST/CET hours», «overlap with US time zones», UTC-3…-8, а у Himalayas — часовые пояса вне UTC+4…+10.
Явный APAC в локации перевешивает. «Visa sponsorship: not available» больше не считается спонсорством.

RemoteOK отдаёт в API только начало описания: такие вакансии помечены «текст неполный, проверить
вручную», а Go у них — «?» (не 0), `--go-only` их не отсеивает.
