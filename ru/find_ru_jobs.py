#!/usr/bin/env python3
"""
find_ru_jobs.py - поиск вакансий Go/Backend (junior/middle, удалёнка) на российском рынке.

Источники (каждый можно выключить в config_ru.yaml):
  habr      Хабр Карьера - официальный RSS career.habr.com/vacancies/rss
  superjob  api.superjob.ru - нужен ключ приложения SUPERJOB_KEY
  trudvsem  «Работа России», открытое API opendata.trudvsem.ru
  telegram  публичные веб-превью каналов t.me/s/<канал> (список каналов - в конфиге)
  yandex    yandex.ru/jobs - публичный JSON карьерного сайта
  vk        team.vk.company - данные страницы поиска (встроенный JSON Next.js)
  mts       job.mts.ru - публичный JSON карьерного сайта

Результат: ru/out/jobs_ru.json, ru/out/report_ru.md; учёт уже виденных вакансий - state/seen_ru.json
(отдельно от state/seen_boards.json скрипта boards/find_boards.py). Учёт лимита JobSync - общий
state/jobsync_ledger.json (лимит 60/час один на все инструменты).

Переменные окружения (все необязательны):
  SUPERJOB_KEY      ключ приложения SuperJob (api.superjob.ru/register)
  DEEPSEEK_API_KEY  ключ DeepSeek для --ai (резюме - resume.txt в корне проекта)
  JOBSYNC_URL, JOBSYNC_TOKEN  для --push-jobsync

Примеры (из корня папки):
  python3 ru/find_ru_jobs.py                         # все включённые источники
  python3 ru/find_ru_jobs.py --sources habr,telegram --days 14
  python3 ru/find_ru_jobs.py --check                 # только проверить доступность источников
  python3 ru/find_ru_jobs.py --ai --ai-limit 20      # с AI-оценкой (платно, только прошедшие фильтры)
  python3 ru/find_ru_jobs.py --push-jobsync --dry-run

Только стандартная библиотека Python 3.9+. PyYAML и certifi используются, если установлены.
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # корень проекта - чтобы работал `from common import ...`

from common import JOBSYNC_LEDGER, ROOT, STATE_DIR  # noqa: E402
from common import ai_score as _ai  # noqa: E402
from common import filters as _filters  # noqa: E402
from common.filters import strip_html  # noqa: E402,F401
from common.httpclient import Http as _Http, HttpError, NetError  # noqa: E402,F401
from common.jobsync_push import build_description, job_header, push_jobs  # noqa: E402
from common.state import load_json, save_json  # noqa: E402

VERSION = "1.0"
UA = f"find_ru_jobs/{VERSION} (personal job search script; python-urllib)"
ALL_SOURCES = ["habr", "superjob", "trudvsem", "telegram", "yandex", "vk", "mts"]


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat() if d else ""


def parse_date(s):
    """ISO-8601 / RFC-822 / unix-время -> aware datetime или None."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(s, dt.timezone.utc)
    s = str(s).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if not m:
                return None
            d = dt.datetime(int(m[1]), int(m[2]), int(m[3]))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))  # российские сайты - обычно МСК
    return d


# =====================================================================
# Конфиг: PyYAML, если есть, иначе встроенный разбор простого YAML
# =====================================================================

def _scalar(v):
    v = v.strip()
    if not v:
        return None
    if v[0] == "'" and v.endswith("'") and len(v) > 1:
        return v[1:-1].replace("''", "'")
    if v[0] == '"' and v.endswith('"') and len(v) > 1:
        return json.loads(v)
    v = re.sub(r"\s+#.*$", "", v)
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def mini_yaml(text):
    """Подмножество YAML: вложенные словари по отступам, списки скаляров через '- '."""
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), raw.strip()))

    def block(i, indent):
        if i < len(lines) and lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                out.append(_scalar(lines[i][1][2:]))
                i += 1
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == indent:
            key, _, val = lines[i][1].partition(":")
            i += 1
            if val.strip():
                out[key.strip()] = _scalar(val)
            elif i < len(lines) and lines[i][0] > indent:
                out[key.strip()], i = block(i, lines[i][0])
            elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                out[key.strip()], i = block(i, indent)
            else:
                out[key.strip()] = None
        return out, i

    res, _ = block(0, lines[0][0]) if lines else ({}, 0)
    return res


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise SystemExit(f"Не найден файл настроек {path}. Укажите его через --config.")
    try:
        import yaml  # необязательно
        return yaml.safe_load(text) or {}
    except ImportError:
        return mini_yaml(text)


# =====================================================================
# HTTP: ретраи, таймауты, паузы по хостам, подсказки про SSL на macOS
# =====================================================================

HTTP_INTERVALS = {  # секунд между запросами к одному хосту
    "career.habr.com": 2.0, "api.superjob.ru": 1.0,
    "opendata.trudvsem.ru": 1.0, "t.me": 2.0, "yandex.ru": 1.5,
    "team.vk.company": 2.0, "job.mts.ru": 1.0,
}


def Http(insecure=False, retries=3, timeout=25):
    """Общий клиент common/httpclient.py с настройками ru-скрипта."""
    return _Http(UA, intervals=HTTP_INTERVALS, default_interval=1.0, retries=retries, timeout=timeout,
                 insecure=insecure, backoff=2.0, retry_after_cap=120, report_errors=False)


# =====================================================================
# Текст и нормализация
# =====================================================================

def norm_key(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\b(ооо|ао|пао|зао|оао|ип|llc|ltd|inc|gmbh)\b", " ", s)
    return re.sub(r"[^a-zа-я0-9]+", " ", s).strip()


def fmt_salary(lo, hi, cur="RUR", gross=None):
    if not lo and not hi:
        return ""
    cur = {"RUR": "₽", "RUB": "₽", "rub": "₽", "USD": "$", "EUR": "€", "": "₽", None: "₽"}.get(cur, cur)
    f = lambda x: f"{int(x):,}".replace(",", " ")
    s = f"{f(lo)}–{f(hi)}" if lo and hi and lo != hi else (f"от {f(lo)}" if lo else f"до {f(hi)}")
    s += f" {cur}"
    if gross is True:
        s += " до вычета"
    elif gross is False:
        s += " на руки"
    return s


def job(source, title, company, url, location="", remote=None, salary="", published=None,
        description="", extra=None):
    d = parse_date(published) if not isinstance(published, dt.datetime) else published
    return {
        "title": (title or "").strip()[:300],
        "company": (company or "").strip()[:200] or "?",
        "url": url,
        "source": source,
        "location": (location or "").strip()[:200],
        "remote": remote,
        "salary": salary or "",
        "published_at": iso(d) if d else "",
        "description": (description or "").strip()[:6000],
        "extra": extra or {},
    }


REMOTE_RX = re.compile(r"удал[её]нн|удал[её]нк|удаленно|удалённо|дистанцион|remote|из любой точки|anywhere|"
                       r"можно удал", re.I)


def text_remote(text):
    return bool(REMOTE_RX.search(text or ""))


# =====================================================================
# Источники. Каждый: fn(http, cfg, scfg, ctx) -> list[job]; ctx["status"] - что произошло
# =====================================================================

class SourceSkipped(Exception):
    """Источник не используется (нет ключа, 403, закрыт) - с объяснением."""


HABR_TITLE = re.compile(r"Требуется «(.+)»\s*(?:\((.*)\))?\s*$")


def src_habr(http, cfg, s, ctx):
    out = []
    qids = s.get("qids") or [None]
    for q in cfg["queries"]:
        for qid in list(qids) + [None]:
            params = {"q": q, "type": "all", "sort": "date"}
            if cfg.get("require_remote", True):
                params["remote"] = "true"
            if qid:
                params["qid"] = qid
            text = http.get("https://career.habr.com/vacancies/rss", params, accept="application/rss+xml")
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                raise NetError("career.habr.com: RSS не разобран")
            for it in root.iter("item"):
                g = lambda t: (it.findtext(t) or "").strip()
                m = HABR_TITLE.match(g("title"))
                title, loc = (m.group(1), m.group(2) or "") if m else (g("title"), "")
                desc = strip_html(g("description"))
                # в скобках заголовка: "Город, от 75 000 до 125 000 ₽" - отделяем зарплату
                sal = re.search(r",?\s*((?:от|до)\s[\d\s]+(?:до\s[\d\s]+)?\s*(?:₽|руб\.?|\$|€))", loc)
                if sal:
                    loc = loc[:sal.start()].strip(" ,")
                out.append(job("habr", title, g("author"), g("link"), loc,
                               remote=("Можно удалённо" in desc) or text_remote(desc),
                               salary=sal.group(1).strip() if sal else "", published=g("pubDate"),
                               description=desc, extra={"habr_qid": qid}))
    return out


def src_superjob(http, cfg, s, ctx):
    key = os.environ.get("SUPERJOB_KEY", "").strip()
    if not key:
        raise SourceSkipped("нет SUPERJOB_KEY (ключ приложения: api.superjob.ru, раздел «Регистрация приложения»)")
    out = []
    for q in cfg["queries"]:
        params = {"keyword": q, "count": s.get("count", 100), "period": s.get("period", 30),
                  "catalogues": s.get("catalogues", 48), "no_agreement": 0}
        if cfg.get("require_remote", True):
            params["place_of_work"] = 2  # 2 = удалённая работа
        try:
            d = http.get_json("https://api.superjob.ru/2.0/vacancies/", params, {"X-Api-App-Id": key})
        except HttpError as e:
            if "country code" in e.body or e.body.lstrip().startswith("<"):
                raise SourceSkipped(f"HTTP {e.code}: сервер SuperJob блокирует запросы с вашего IP/страны (WAF). "
                                    "Обход не делается; попробуйте с другого подключения.")
            raise SourceSkipped(f"HTTP {e.code}: {e.body[:200]}")
        for v in d.get("objects", []):
            out.append(job(
                "superjob", v.get("profession"), v.get("firm_name"), v.get("link"),
                (v.get("town") or {}).get("title"),
                remote=((v.get("place_of_work") or {}).get("id") == 2) or None,
                salary=fmt_salary(v.get("payment_from"), v.get("payment_to"), v.get("currency")),
                published=v.get("date_published"),
                description=strip_html(v.get("candidat") or v.get("vacancyRichText") or ""),
                extra={"experience": (v.get("experience") or {}).get("title", "")},
            ))
    return out


def src_trudvsem(http, cfg, s, ctx):
    out = []
    for q in cfg["queries"]:
        d = http.get_json("https://opendata.trudvsem.ru/api/v1/vacancies",
                          {"text": q, "limit": s.get("limit", 100), "offset": 0})
        for w in (d.get("results") or {}).get("vacancies") or []:
            v = w.get("vacancy") or {}
            desc = strip_html("\n".join(filter(None, [v.get("requirements"), v.get("duty"),
                                                      (v.get("addresses") or {}).get("address", [{}])[0].get("location", "")
                                                      if isinstance(v.get("addresses"), dict) else ""])))
            remote = text_remote(" ".join([v.get("schedule") or "", v.get("employment") or "", desc]))
            out.append(job(
                "trudvsem", v.get("job-name"), (v.get("company") or {}).get("name"), v.get("vac_url"),
                (v.get("region") or {}).get("name"), remote=remote or None,
                salary=fmt_salary(v.get("salary_min"), v.get("salary_max")),
                published=v.get("creation-date"), description=desc,
            ))
    return out


TG_GO = re.compile(r"golang|#go\b|\bgo[- ]?(?:разраб|developer|engineer|инженер|программист|backend)|"
                   r"backend[^\n]{0,40}\bgo\b|\bgo\b[^\n]{0,25}(?:middle|junior)", re.I)
TG_JOB = re.compile(r"вакансi?|ваканси|ищем|ищу|hiring|требуется|#job|#вакансия|зарплат|вилка|salary|"
                    r"обязанност|требовани|responsib", re.I)


TG_BRANDS = [  # известные работодатели: название в тексте поста -> как показать
    (r"yandex|яндекс", "Яндекс"), (r"\bozon\b|озон", "Ozon"), (r"avito|авито", "Авито"),
    (r"\bvk\b|вконтакте|\bvk tech\b", "VK"), (r"сбер|sber", "Сбер"), (r"т-банк|t-bank|тинькофф|tinkoff", "Т-Банк"),
    (r"\bмтс\b|\bmts\b", "МТС"), (r"wildberries|вайлдберриз|\bwb\b", "Wildberries"), (r"kaspersky|касперск", "Лаборатория Касперского"),
    (r"альфа-банк|alfa-bank", "Альфа-Банк"), (r"\bвтб\b|\bvtb\b", "ВТБ"), (r"\bx5\b", "X5"), (r"2gis|2гис", "2ГИС"),
    (r"selectel|селектел", "Selectel"), (r"kontur|контур", "Контур"), (r"lamoda|ламода", "Lamoda"),
]
_TG_STOP = re.compile(r"(?i)^(вакансия|vacancy|job|go|golang|remote|middle|junior|senior|backend|разработчик|"
                      r"developer|engineer|команду|команде|компанию|компании|офис|удал[её]нн\w*|it|ит)$")


def _clean_company(c):
    c = re.sub(r"\s+", " ", c).strip(" *:-—–«»\"'.,!()[]#@")
    return c if 2 <= len(c) <= 80 and not _TG_STOP.match(c) else ""


def tg_company(text, channel, title=""):
    """Название компании из текста поста; если его нет - '' (тогда в отчёте пишется канал)."""
    pats = [
        r"(?im)^\W*(?:компания|company|работодатель|employer|о компании)\s*[:：—–-]\s*(.+)$",
        r"(?im)^\W*(?:компания|работодатель)\s+[«\"]([^»\"\n]{2,60})[»\"]",
        r"(?i)\b(?:в|к)\s+(?:компанию|компании|команду|команде|стартап|продуктовую компанию)\s+[«\"]?([A-ZА-ЯЁ0-9][^\n«»\".,!:;()]{1,50})",
        r"(?m)^\W*([A-ZА-ЯЁ][\w&.\- ]{1,40}?)\s+(?:ищет|в поиске|находится в поиске|is hiring|hiring|расширяет команду)\b",
        r"(?i)(?:^|\n)\W*(?:мы|we are|we're)\s*[—–-]\s*([A-ZА-ЯЁ][^\n,.]{1,50})",
        r"(?i)\b(?:at|в)\s+([A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+){0,2})\s*$",  # "... в Yandex Cloud"
    ]
    for n, rx in enumerate(pats):
        src = title if n == len(pats) - 1 else text
        m = re.search(rx, src)
        if m:
            c = _clean_company(m.group(1))
            if c:
                return c
    head = f"{title}\n{text[:600]}"
    for rx, name in TG_BRANDS:
        if re.search(rx, head, re.I):
            return name
    last = text.strip().splitlines()[-1] if text.strip() else ""
    for h in re.findall(r"#([A-Za-zА-Яа-яЁё0-9_]{2,40})", last):  # хэштег компании в конце поста
        if _clean_company(h) and not re.search(r"(?i)remote|удал|офис|гибрид|^go|golang|backend|бэкенд|вакан|job|"
                                                 r"middle|junior|senior|разраб|dev|fulltime|full_time|зп|salary", h):
            return h
    return ""


def tg_title(text):
    for line in text.splitlines():
        line = re.sub(r"^[^\w«\"(]+", "", line).strip(" *#")
        if len(line) >= 6 and not re.match(r"(?i)^(вакансия|#)", line):
            return re.sub(r"\s+", " ", line)[:160]
    return text[:120]


def src_telegram(http, cfg, s, ctx):
    out = []
    channels = s.get("channels") or []
    if not channels:
        raise SourceSkipped("список каналов пуст (sources.telegram.channels в конфиге)")
    for ch in channels:
        ch = ch.lstrip("@")
        before = None
        for _ in range(int(s.get("pages", 1))):
            url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
            try:
                text = http.get(url, accept="text/html")
            except HttpError as e:
                ctx["notes"].append(f"@{ch}: HTTP {e.code}")
                break
            if "tgme_widget_message_wrap" not in text:
                ctx["notes"].append(f"@{ch}: веб-превью недоступно (канал закрыт или превью отключено)")
                break
            ids = []
            for b in text.split('class="tgme_widget_message_wrap')[1:]:
                post = re.search(r'data-post="([^"]+)"', b)
                body = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
                when = re.search(r'datetime="([^"]+)"', b)
                if not post:
                    continue
                pid = post.group(1)
                if pid.split("/")[-1].isdigit():
                    ids.append(int(pid.split("/")[-1]))
                if not body:
                    continue
                t = strip_html(body.group(1))
                if not (TG_GO.search(t) and TG_JOB.search(t)):
                    continue
                if re.search(r"(?i)дайджест|подборк|digest", t[:200]) or len(re.findall(r"https?://", t)) > 6:
                    continue  # сводные посты со многими вакансиями - не одна вакансия
                title = tg_title(t)
                comp = tg_company(t, ch, title)
                out.append(job("telegram", title, comp or f"не указана (@{ch})", f"https://t.me/{pid}",
                               remote=text_remote(t) or None, published=when.group(1) if when else None,
                               description=t, extra={"channel": ch, "company_from_text": bool(comp)}))
            if not ids:
                break
            before = min(ids)
    return out


def src_yandex(http, cfg, s, ctx):
    out, seen = [], set()
    reqs = [{"skills": sk} for sk in s.get("skills") or []] + [{"text": q} for q in cfg["queries"]]
    for base in reqs:
        for page in range(1, int(s.get("max_pages", 1)) + 1):
            params = dict(base, page_size=40, page=page)
            if cfg.get("require_remote", True):
                params["work_modes"] = "remote"
            d = http.get_json("https://yandex.ru/jobs/api/publications", params)
            res = d.get("results") or []
            for p in res:
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                v = p.get("vacancy") or {}
                modes = [m.get("slug") for m in v.get("work_modes") or []]
                svc = (p.get("public_service") or {}).get("name") or ""
                out.append(job(
                    "yandex", p.get("title"), f"Яндекс ({svc})" if svc else "Яндекс",
                    f"https://yandex.ru/jobs/vacancies/{p.get('publication_slug_url')}",
                    ", ".join(c.get("name", "") for c in v.get("cities") or []),
                    remote="remote" in modes, published=p.get("modified"),
                    description=p.get("short_summary") or "",
                    extra={"skills": [x.get("name") for x in v.get("skills") or []]},
                ))
            if not res or page * 40 >= int(d.get("count") or 0):
                break
    return out


def src_vk(http, cfg, s, ctx):
    out, seen = [], set()
    for q in cfg["queries"]:
        params = {"search": q}
        if cfg.get("require_remote", True):
            params["remote"] = "true"
        text = http.get("https://team.vk.company/vacancy/", params, accept="text/html")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
        if not m:
            raise NetError("team.vk.company: на странице нет данных __NEXT_DATA__ (сайт изменился)")
        pp = (json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}
        for v in pp.get("initialVacancies") or []:
            if v.get("id") in seen:
                continue
            seen.add(v.get("id"))
            grp = (v.get("group") or {}).get("name") or ""
            tags = [t.get("name") for t in v.get("tags") or []]
            out.append(job(
                "vk", strip_html(v.get("title")), f"VK ({grp})" if grp else "VK",
                f"https://team.vk.company/vacancy/{v.get('id')}/", (v.get("town") or {}).get("name"),
                remote=bool(v.get("remote")),
                description=f"Направление: {(v.get('specialty') or {}).get('name', '')}. Формат: "
                            f"{v.get('work_format') or ''}. Теги: {', '.join(filter(None, tags))}",
            ))
    return out


def src_mts(http, cfg, s, ctx):
    # robots.txt job.mts.ru запрещает URL с search=/filters=, поэтому берём общий список постранично
    out = []
    pages = int(s.get("max_pages", 25))
    for page in range(1, pages + 1):
        d = http.get_json("https://job.mts.ru/api/v2/vacancies",
                          {"pagination[page]": page, "pagination[pageSize]": 100})
        for v in d.get("data") or []:
            title = v.get("title") or ""
            if not title_relevant(title, cfg):  # фильтр сразу: в списке тысячи вакансий
                continue
            fmts = [w.get("title", "") for w in v.get("workFormats") or []]
            reqs = v.get("requirements") or []
            desc = "; ".join(r if isinstance(r, str) else str(r.get("text") or r.get("title") or "")
                             for r in reqs) if isinstance(reqs, list) else str(reqs)
            out.append(job(
                "mts", title, (v.get("organization") or {}).get("title") or "МТС",
                f"https://job.mts.ru/vacancy/{v.get('slug')}", (v.get("region") or {}).get("title"),
                remote=text_remote(" ".join(fmts)) or None,
                salary=fmt_salary(v.get("salaryFrom"), v.get("salaryTo"), v.get("currency") or "RUR"),
                published=v.get("publishedAt") or v.get("createdAt"),
                description=desc or strip_html(v.get("description") or ""),
                extra={"experience": (v.get("experience") or {}).get("title", ""), "work_formats": fmts},
            ))
        pc = ((d.get("meta") or {}).get("pagination") or {}).get("pageCount") or 0
        if page >= pc:
            break
    return out


SOURCES = {"habr": src_habr, "superjob": src_superjob, "trudvsem": src_trudvsem,
           "telegram": src_telegram, "yandex": src_yandex, "vk": src_vk, "mts": src_mts}


# =====================================================================
# Фильтры и эвристическая оценка
# =====================================================================

def _rx_any(patterns):
    pats = [p for p in patterns or [] if p]
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None


def compile_filters(cfg):
    cfg["_inc"] = _rx_any(cfg.get("title_include"))
    cfg["_exc"] = _rx_any(cfg.get("title_exclude"))
    cfg["_lvl"] = _rx_any(cfg.get("level_ok"))
    cfg["_big"] = [norm_key(c) for c in cfg.get("big_companies") or []]
    langs = cfg.get("also_ok_langs") or []
    cfg["_lang"] = _rx_any([rf"\b{re.escape(x)}\b" for x in langs])


GO_RX = re.compile(r"golang|\bgo\b", re.I)
SENIOR_RX = re.compile(r"senior|старш|ведущ|lead|лид\b|#senior|#lead", re.I)
# senior в заголовке отсекает всегда, даже вместе с middle ("Middle+/Senior" - это вакансия на senior)
SENIOR_TITLE_RX = re.compile(r"senior|сеньор|синьор|\bsr\b|\blead\b|тимлид|техлид|\bлид\b|ведущ|старш|"
                             r"principal|staff\b|head\b", re.I)
# другие основные языки: если в заголовке они есть рядом с Go, Go - не основной язык
OTHER_LANG_RX = re.compile(r"\boracle\b|pl/?sql|delphi|\bjava\b(?!script)|kotlin|scala|\bphp\b|c#|\.net|"
                           r"\b1с\b|\b1c\b|ruby|c\+\+|\bnode(?:\.?js)?\b|typescript|javascript|\bvue|react|"
                           r"angular|swift|rust|python|perl|elixir|erlang|clojure|abap|битрикс|bitrix", re.I)
# ограничения для работы не из России: показываются в отчёте (оценку не меняют)
RF_LIMITS = [
    ("только РФ", re.compile(r"только\s+(?:из|в|по|на\s+территории)\s+(?:рф|росси)|удал[её]нн\w*\s*\(\s*рф\s*\)|"
                             r"удал[её]нн\w*\s+(?:работа\s+)?по\s+(?:рф|росси)|из\s+любого\s+города\s+росси|"
                             r"находит\w*\s+(?:в|на\s+территории)\s+(?:рф|росси)|на\s+территории\s+(?:рф|росси)|"
                             r"в\s+пределах\s+(?:рф|росси)|не\s+(?:рассматрива\w+|подходит)\s+[^.\n]{0,40}(?:релокант|"
                             r"за\s+границ|за\s+рубеж|вне\s+(?:рф|росси))|russia only|only in russia", re.I)),
    ("гражданство РФ", re.compile(r"гражданств\w*\s+(?:рф|росси)|граждан\w*\s+(?:рф|росси)", re.I)),
    ("резидент РФ", re.compile(r"(?:налогов\w+\s+)?резидент\w*\s+(?:рф|росси)|нерезидент", re.I)),
    ("оформление по ТК РФ", re.compile(r"\bтк\s+рф\b|трудов\w+\s+кодекс|оформлени\w*\s+по\s+тк|"
                                       r"официальн\w+\s+оформлени|трудоустройств\w*\s+по\s+тк", re.I)),
    ("допуск/гостайна", re.compile(r"гостайн|форм\w*\s+допуск|допуск\w*\s+к\s+(?:сведениям|гос)", re.I)),
]
RF_HARD = {"только РФ", "гражданство РФ", "резидент РФ", "допуск/гостайна"}


def rf_limits(j):
    text = f"{j['title']}\n{j['location']}\n{j['description']}"
    return [name for name, rx in RF_LIMITS if rx.search(text)]


def go_role(j):
    """2 - Go основной язык, 1 - Go упомянут, но не основной (рядом другие языки), 0 - Go нет."""
    t, text = j["title"], f"{j['title']} {j['description']}"
    tags = " ".join((j.get("extra") or {}).get("skills") or [])
    if not GO_RX.search(text + " " + tags):
        return 0
    if GO_RX.search(t):
        # "Oracle developer (+Delphi+Vue+Go)": Go в скобках/после "+" рядом с другими языками
        others = OTHER_LANG_RX.findall(t)
        if others and (re.search(r"[+(/,]\s*(?:golang|go)\b", t, re.I) or
                       OTHER_LANG_RX.search(t).start() < GO_RX.search(t).start()):
            return 1
        return 2
    if OTHER_LANG_RX.search(t):
        return 1  # "Java developer", а Go только в тексте
    # Хабр: теги навыков в тексте - если других языков больше, чем Go, Go не основной
    others = {m.lower() for m in OTHER_LANG_RX.findall(j["description"][:1500])}
    return 1 if len(others) >= 3 else 2


def title_relevant(title, cfg):
    t = (title or "").replace("ё", "е")
    return bool(cfg["_inc"] and cfg["_inc"].search(t))


def years_required(text):
    """Наибольшее минимальное требование опыта в годах (0, если не сказано). Общий разбор - common/filters.py."""
    return _filters.years_required(text) or 0


def reject_reason(j, cfg, max_age):
    t = j["title"].replace("ё", "е")
    text = f"{j['title']}\n{j['description']}"
    if not title_relevant(t, cfg):
        # Telegram/описание: заголовок поста может быть произвольным - смотрим Go в тексте
        if not (j["source"] == "telegram" and GO_RX.search(text)):
            return "заголовок не про Go/backend"
    if SENIOR_TITLE_RX.search(t):
        return "уровень senior/lead в заголовке"
    if cfg["_exc"] and cfg["_exc"].search(t) and not (cfg["_lvl"] and cfg["_lvl"].search(t)):
        return "роль по заголовку (не backend)"
    if not GO_RX.search(text) and not (cfg["_lang"] and cfg["_lang"].search(text)) and j["source"] not in ("yandex", "vk"):
        return "нет Go/подходящего языка в тексте"
    if j["source"] == "habr" and re.search(r"#(senior|lead)\b", j["description"], re.I) \
            and not re.search(r"#(junior|middle)\b", j["description"], re.I):
        return "уровень senior/lead по тегам Хабра"
    exp = (j.get("extra") or {}).get("experience") or ""
    if re.search(r"более 6|больше 6|6 лет и более|от 6", exp, re.I):
        return "опыт более 6 лет"
    y = years_required(text)
    lim = cfg.get("max_years_required")
    if lim and y > int(lim):
        return f"требуется опыт от {y} лет"
    if cfg.get("require_remote", True) and j["remote"] is not True:
        if j["remote"] is None and text_remote(text):
            j["remote"] = True
        else:
            return "не удалёнка"
    if max_age and j["published_at"]:
        d = parse_date(j["published_at"])
        if d and (now_utc() - d).days > max_age:
            return f"старше {max_age} дн."
    return None


def is_big(j, cfg):
    c = norm_key(j["company"])
    return any(b and b in c for b in cfg["_big"])


def heuristic(j, cfg):
    """Эвристика 0-10. Основной вклад - Go как ОСНОВНОЙ язык; Go в списке рядом с Oracle/Delphi/Java - штраф.
    j["go_role"] должен быть посчитан заранее. Ограничения "только РФ" и т.п. оценку не меняют - только показываются."""
    t, text = j["title"], f"{j['title']} {j['description']}"
    s = 4.0
    g = j.get("go_role", 0)
    if g == 2:
        s += 2.5 if GO_RX.search(t) else 1.5
    elif g == 1:
        s -= 1.0  # Go не основной язык
    if re.search(r"junior|middle|джун|мидл|#junior|#middle", text, re.I):
        s += 1
    if SENIOR_RX.search(text) and not re.search(r"junior|middle", t, re.I):
        s -= 1
    if j["remote"]:
        s += 0.5
    if j["salary"]:
        s += 0.5
    if j.get("big"):
        s -= 1
    return round(max(0.0, min(10.0, s)), 1)


# =====================================================================
# Состояние между запусками (seen_ru.json)
# =====================================================================

def pair_key(j):
    return f"{norm_key(j['company'])}|{norm_key(j['title'])}"


def load_seen(path):
    d = load_json(path, {})
    return {"urls": d.get("urls", {}), "pairs": d.get("pairs", {}), "ai": d.get("ai", {})}


# =====================================================================
# AI-оценка (DeepSeek, OpenAI-совместимый chat/completions)
# =====================================================================

def ai_score(http, cfg, jobs_list, seen, limit):
    """AI-оценка - общий модуль common/ai_score.py (кэш state/ai_scores.json, резюме resume.txt в корне)."""
    ai = cfg.get("ai") or {}
    rf = ai.get("resume_file") or "resume.txt"
    path = rf if os.path.isabs(rf) else os.path.join(ROOT, rf)
    return _ai.score_jobs(http, jobs_list, limit=limit, settings=ai, resume_path=path, extra_cache=seen.get("ai"))


# =====================================================================
# JobSync через MCP
# =====================================================================

def jobsync_item(j, max_desc=15000):
    tags = ["Go"] if GO_RX.search(f"{j['title']} {j['description']}") else []
    tags += ["RU", j["source"]]
    header = job_header(j)
    item = {"company": j["company"][:200], "jobTitle": j["title"][:300],
            "jobDescription": build_description(j["description"], header, j["url"], max_desc),
            "source": f"find_ru_jobs:{j['source']}", "jobUrl": j["url"], "tags": tags[:10], "upsert": True}
    if j["location"]:
        item["location"] = j["location"]
    if j["remote"]:
        item["workplaceType"] = "Remote"
    return item


def push_jobsync(http, cfg, jobs_list, a):
    """Отправка в JobSync через общий клиент common/jobsync_push.py (общий учёт лимита)."""
    jc = cfg.get("jobsync") or {}
    min_score = a.push_min_score if a.push_min_score is not None else float(jc.get("min_score", 6))
    check = bool(jc.get("check_duplicates", True)) and not a.no_dup_check
    ledger_path = jc.get("ledger") or JOBSYNC_LEDGER
    if not os.path.isabs(ledger_path):
        ledger_path = os.path.join(STATE_DIR, os.path.basename(ledger_path))
    from common.jobsync_push import Ledger
    ledger = Ledger(ledger_path)
    todo = [j for j in jobs_list if j["score"] >= min_score and j["url"] not in ledger.pushed][: a.push_limit]
    log(f"JobSync: порог оценки {min_score}")
    push_jobs(todo, jobsync_item, budget=int(jc.get("budget_per_hour", 50)), check_existing=check,
              dry_run=a.dry_run, ledger=ledger, http=http, client_name="find_ru_jobs",
              describe=lambda j: f"{j['score']:>4} {j['company']} — {j['title']}")


# =====================================================================
# Отчёт
# =====================================================================

def md(s):
    return re.sub(r"([|\[\]])", r"\\\1", (s or "").replace("\n", " "))


def zero_reason(st):
    """Почему источник не дал новых вакансий - одной строкой для отчёта."""
    if st["status"] != "работает":
        return st["status"]
    if st["raw"] == 0:
        return "источник работает, но по запросам ничего не вернул"
    if st["passed"] == 0:
        top = sorted(st["reasons"].items(), key=lambda x: -x[1])[:3]
        return f"получено {st['raw']}, все отсеяны фильтрами: " + "; ".join(f"{k} — {v}" for k, v in top)
    return (f"получено {st['raw']}, прошло фильтры {st['passed']}, но все уже были в прошлых запусках "
            f"(показать снова: --all)")


def write_report(path, jobs_list, stats, run_at, args, ai_used):
    L = [f"# Вакансии RU — {run_at.astimezone().strftime('%Y-%m-%d %H:%M')}", ""]
    L.append(f"Новых вакансий после фильтров: **{len(jobs_list)}**. "
             f"Оценка: {'AI (DeepSeek) + эвристика' if ai_used else 'эвристика (без --ai)'}.")
    L += ["", "## Источники", "", "| Источник | Статус | Получено | Прошло фильтры | Новых |", "|---|---|---|---|---|"]
    for name, st in stats.items():
        L.append(f"| {name} | {md(st['status'])} | {st['raw']} | {st['passed']} | {st['new']} |")
    zero = [(n, st) for n, st in stats.items() if st["new"] == 0]
    if zero:
        L += ["", "## Источники, которые дали 0, и почему", ""]
        for n, st in zero:
            L.append(f"- **{n}**: {zero_reason(st)}")
    L += ["", "## Вакансии (по убыванию оценки)", ""]
    if not jobs_list:
        L.append("_Новых подходящих вакансий нет._")
    for n, j in enumerate(jobs_list, 1):
        bits = [j["source"], j["location"] or "", "удалёнка" if j["remote"] else "", j["salary"],
                j["published_at"][:10], "крупная" if j.get("big") else "",
                "Go не основной язык" if j.get("go_role") == 1 else ""]
        kind = "AI" if j.get("ai") else "эвр."
        L.append(f"{n}. **[{md(j['title'])}]({j['url']})** — {md(j['company'])} · **{j['score']}** ({kind})")
        L.append(f"   - {' · '.join(b for b in bits if b)}")
        if j.get("rf_limits"):
            L.append("   - ⚠️ ограничения: " + ", ".join(("⛔ " if x in RF_HARD else "") + x for x in j["rf_limits"]))
        if j.get("ai"):
            L.append(f"   - {md(_ai.fmt_ai(j['ai']))}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# =====================================================================
# main
# =====================================================================

class RuHelp(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, "использование: ")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=RuHelp, add_help=False)
    p._optionals.title = "параметры"
    p.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")
    p.add_argument("--config", default=os.path.join(HERE, "config_ru.yaml"), help="файл настроек (ru/config_ru.yaml)")
    p.add_argument("--sources", help=f"какие источники опросить, через запятую: {','.join(ALL_SOURCES)}")
    p.add_argument("--query", action="append", help="поисковый запрос (можно несколько раз); заменяет queries из конфига")
    p.add_argument("--days", type=int, help="не старше N дней (по умолчанию max_age_days из конфига)")
    p.add_argument("--any-format", action="store_true", help="не требовать удалёнку")
    p.add_argument("--all", action="store_true", help="показывать и уже виденные в прошлых запусках")
    p.add_argument("--check", action="store_true", help="только проверить источники и показать статистику, без записи файлов")
    p.add_argument("--ai", action="store_true", help="AI-оценка через DeepSeek (DEEPSEEK_API_KEY, resume.txt в корне; общий модуль common/ai_score.py)")
    p.add_argument("--ai-limit", type=int, help="максимум платных AI-запросов за запуск (ai.max_calls в конфиге)")
    p.add_argument("--push-jobsync", action="store_true", help="добавить вакансии выше порога в JobSync через MCP")
    p.add_argument("--push-min-score", type=float, help="порог оценки для JobSync (jobsync.min_score)")
    p.add_argument("--push-limit", type=int, default=30, help="сколько вакансий отправить за запуск (30)")
    p.add_argument("--no-dup-check", action="store_true", help="не вызывать find_job перед добавлением (экономит лимит)")
    p.add_argument("--dry-run", action="store_true", help="для --push-jobsync: показать, что ушло бы, без отправки")
    p.add_argument("--out", default=os.path.join(HERE, "out", "jobs_ru.json"),
                   help="файл вакансий JSON (ru/out/jobs_ru.json)")
    p.add_argument("--report", default=os.path.join(HERE, "out", "report_ru.md"),
                   help="отчёт Markdown (ru/out/report_ru.md)")
    p.add_argument("--seen", default=os.path.join(STATE_DIR, "seen_ru.json"),
                   help="учёт виденных вакансий между запусками (state/seen_ru.json)")
    p.add_argument("--insecure", action="store_true", help="не проверять SSL-сертификаты (крайний случай)")
    p.add_argument("-v", "--verbose", action="store_true", help="показывать причины отсева")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if sys.version_info < (3, 9):
        raise SystemExit("Нужен Python 3.9+")
    cfg = load_config(a.config)
    if a.query:
        cfg["queries"] = a.query
    cfg.setdefault("queries", ["golang"])
    if a.any_format:
        cfg["require_remote"] = False
    compile_filters(cfg)
    max_age = a.days if a.days is not None else cfg.get("max_age_days")
    scfg_all = cfg.get("sources") or {}
    if a.sources:
        names = [s.strip() for s in a.sources.split(",") if s.strip()]
        bad = [s for s in names if s not in SOURCES]
        if bad:
            raise SystemExit(f"Неизвестные источники: {', '.join(bad)}. Доступны: {', '.join(ALL_SOURCES)}")
    else:
        names = [s for s in ALL_SOURCES if (scfg_all.get(s) or {}).get("enabled", True)]

    http = Http(insecure=a.insecure)
    run_at = now_utc()
    seen = load_seen(a.seen)
    stats, collected = {}, []
    for name in names:
        ctx = {"notes": []}
        log(f"→ {name} ...")
        t0 = time.monotonic()
        try:
            got = SOURCES[name](http, cfg, scfg_all.get(name) or {}, ctx)
            status = "работает"
        except SourceSkipped as e:
            got, status = [], f"пропущен: {e}"
        except HttpError as e:
            body = re.sub(r"<[^>]+>|\s+", " ", e.body)[:150].strip()
            got, status = [], f"ошибка HTTP {e.code}: {body}"
        except NetError as e:
            got, status = [], f"ошибка сети: {e}"
        except Exception as e:  # чтобы один сломанный источник не ронял остальные
            got, status = [], f"сбой разбора: {type(e).__name__}: {e}"
        if ctx["notes"]:
            status += " (" + "; ".join(ctx["notes"]) + ")"
        stats[name] = {"status": status, "raw": len(got), "passed": 0, "new": 0, "reasons": {}, "dups": 0}
        log(f"  {name}: {status}; получено {len(got)} за {time.monotonic() - t0:.0f}с")
        collected += got

    # фильтры + дедупликация внутри запуска (по URL и паре компания+название)
    by_url, by_pair, kept = set(), {}, []
    reasons = {}
    for j in collected:
        r = reject_reason(j, cfg, max_age)
        if r:
            reasons[r] = reasons.get(r, 0) + 1
            sr = stats[j["source"]]["reasons"]
            sr[r] = sr.get(r, 0) + 1
            if a.verbose:
                log(f"  - [{j['source']}] {j['title']} ({j['company']}): {r}")
            continue
        pk = pair_key(j)
        if j["url"] in by_url:
            stats[j["source"]]["dups"] += 1
            continue
        stats[j["source"]]["passed"] += 1
        if pk in by_pair:  # та же вакансия из другого источника - запоминаем вторую ссылку
            by_pair[pk].setdefault("extra", {}).setdefault("also", []).append(j["url"])
            continue
        by_url.add(j["url"])
        by_pair[pk] = j
        kept.append(j)
    if reasons:
        log("Отсеяно: " + "; ".join(f"{k} — {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))

    fresh = []
    for j in kept:
        old = j["url"] in seen["urls"] or pair_key(j) in seen["pairs"]
        if old and not a.all:
            continue
        j["new"] = not old
        j["big"] = is_big(j, cfg)
        j["go_role"] = go_role(j)
        j["rf_limits"] = rf_limits(j)
        j["score"] = heuristic(j, cfg)
        stats[j["source"]]["new"] += 1
        fresh.append(j)

    ai_used = False
    if a.ai and fresh and not a.check:
        fresh.sort(key=lambda j: -j["score"])
        limit = a.ai_limit if a.ai_limit is not None else int((cfg.get("ai") or {}).get("max_calls", 40))
        ai_score(http, cfg, fresh, seen, limit)
        ai_used = True
        for j in fresh:
            if j.get("ai"):
                j["heuristic"] = j["score"]
                j["score"] = float(j["ai"]["score"])
    fresh.sort(key=lambda j: j["published_at"] or "", reverse=True)
    fresh.sort(key=lambda j: -j["score"])  # стабильная сортировка: по оценке, внутри - свежие выше

    log("")
    log(f"{'Источник':<10} {'получено':>8} {'прошло':>7} {'новых':>6}  статус")
    for n, st in stats.items():
        log(f"{n:<10} {st['raw']:>8} {st['passed']:>7} {st['new']:>6}  {st['status'][:110]}")
    for n, st in stats.items():
        if st["new"] == 0:
            log(f"  0 из {n}: {zero_reason(st)[:160]}")
    log(f"Итого новых после фильтров и дедупликации: {len(fresh)}")
    if a.check:
        for j in fresh[:15]:
            log(f"  {j['score']:>4}  [{j['source']}] {j['title']} — {j['company']}  {j['url']}")
        return 0

    out = [{k: v for k, v in j.items()} for j in fresh]
    save_json(a.out, out)
    os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
    write_report(a.report, fresh, stats, run_at, a, ai_used)
    stamp = iso(run_at)
    for j in fresh:
        seen["urls"].setdefault(j["url"], stamp)
        seen["pairs"].setdefault(pair_key(j), stamp)
    save_json(a.seen, seen)
    log(f"Записано: {a.out}, {a.report}; учёт: {a.seen}")

    if a.push_jobsync:
        push_jobsync(http, cfg, fresh, a)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\nПрервано.")
        sys.exit(130)
