#!/usr/bin/env python3
"""
find_boards.py - ищет небольшие компании на Greenhouse / Lever / Ashby с подходящими
вакансиями (Go/Python backend, Junior/Middle, remote или релокация) и готовит списки
ссылок на борды для автоматизаций JobSync (по 25 на провайдера).

Только стандартная библиотека Python 3.9+ (certifi подхватывается, если установлен).

Источники кандидатов:
  --hn / --hn-months N / --hn-thread ID   темы "Ask HN: Who is hiring?"
  --sources remoteok,remotive,himalayas,arbeitnow,jobicy   публичные API вакансий
  --seed файл                             названия, токены или ссылки на борды

Результат (по умолчанию в boards/out/):
  boards_greenhouse.txt, boards_lever.txt, boards_ashby.txt   - по 25 ссылок для JobSync
  report.md       - отчёт: компания, провайдер, подходящие вакансии, remote/Go, ссылка
  boards.json     - все найденные борды с деталями
  jobs_noats.json - подходящие вакансии у компаний без борда (для common/jobsync_push.py)
Состояние между запусками: state/seen_boards.json, кэш ответов: state/cache/boards/.

Remote-вакансии по умолчанию подходят, только если они worldwide/anywhere/APAC/Asia
(или регион не указан); "remote только США/Индия/Европа" отсеиваются (--remote-regions).

Примеры (из корня папки):
  python3 boards/find_boards.py --hn-thread 50338728 --remote-only
  python3 boards/find_boards.py --hn --hn-months 3 --sources all --remote-only --watchlist
  python3 boards/find_boards.py --seed companies.txt --max-jobs 40
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # корень проекта - чтобы работал `from common import ...`

from common import ROOT, STATE_DIR  # noqa: E402
from common import ai_score as AI  # noqa: E402
from common.filters import DEFAULT_MAX_YEARS, clean_text, strip_html, years_required  # noqa: E402
from common.httpclient import Http, NetError, NotFound  # noqa: E402
from common.state import Cache, load_json, save_json  # noqa: E402

VERSION = "2.0"
UA = f"find_boards/{VERSION} (personal job-search script; python-urllib)"
PROVIDERS = ("greenhouse", "lever", "ashby")
BOARD_URL = {  # формат ссылок, который принимает JobSync
    "greenhouse": "https://boards.greenhouse.io/{t}",
    "lever": "https://jobs.lever.co/{t}",
    "ashby": "https://jobs.ashbyhq.com/{t}",
}
JOBSYNC_LIMIT = 25


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def age_days(iso):
    try:
        return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def to_iso(v):
    """Дата публикации из API (ISO-строка, unix-секунды или миллисекунды) -> ISO UTC или ''."""
    if v in (None, "", 0):
        return ""
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            v = float(v)
            d = dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.timezone.utc)
        else:
            d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# =====================================================================
# HTTP: общий клиент из common/httpclient.py (ретраи, паузы по хостам, SSL-подсказки)
# =====================================================================

HTTP_INTERVALS = {  # пауза между запросами к одному хосту, сек
    "remotive.com": 2.0, "remoteok.com": 2.0, "himalayas.app": 1.0,
    "www.arbeitnow.com": 1.0, "jobicy.com": 1.0,
    "jobs.lever.co": 0.5, "jobs.ashbyhq.com": 0.5,
}
HTTP = None  # создаётся в main()


# =====================================================================
# Текст: HTML, нормализация названий, домены, ссылки на ATS
# =====================================================================

LEGAL_WORDS = {"inc", "llc", "ltd", "gmbh", "corp", "co", "corporation", "company", "ag", "sa", "bv",
               "plc", "srl", "sas", "oy", "ab", "as", "pty", "limited", "the"}
SOFT_WORDS = {"labs", "lab", "hq", "ai", "io", "app", "technologies", "technology", "tech", "software",
              "systems", "group", "studio", "studios", "inc", "health", "data", "cloud", "security",
              "robotics", "solutions", "network", "networks", "global", "digital", "dev"}


def name_words(name):
    n = html.unescape(name or "").lower()
    n = re.sub(r"\(.*?\)|\[.*?\]", " ", n)  # (YC W22), [remote]
    n = n.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", n)
    return [w for w in words if w not in LEGAL_WORDS]


def norm_name(name):
    """'Acme Labs, Inc.' -> 'acme' (ядро названия для сравнения)."""
    w = name_words(name)
    core = [x for x in w if x not in SOFT_WORDS] or w
    return "".join(core)


def full_norm(name):
    return "".join(name_words(name))


def slug_variants(name):
    """'Acme Robotics, Inc.' -> ['acmerobotics', 'acme-robotics', 'acme']."""
    w = name_words(name)
    if not w:
        return []
    core = [x for x in w if x not in SOFT_WORDS] or w
    out = ["".join(w), "-".join(w), "".join(core), "-".join(core)]
    if len(w) > 1 and len(w[0]) >= 4:
        out.append(w[0])
    return [v for v in dict.fromkeys(out) if 3 <= len(v) <= 50]


def reg_domain(host):
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


NON_COMPANY_DOMAINS = {
    "ycombinator.com", "github.com", "linkedin.com", "twitter.com", "x.com", "notion.site", "notion.so",
    "google.com", "forms.gle", "calendly.com", "wellfound.com", "angel.co", "workable.com", "lever.co",
    "greenhouse.io", "ashbyhq.com", "bamboohr.com", "recruitee.com", "breezy.hr", "smartrecruiters.com",
    "workatastartup.com", "medium.com", "youtube.com", "bit.ly", "typeform.com", "airtable.com",
    "docs.google.com", "teamtailor.com", "personio.de", "personio.com", "jobs.ashbyhq.com", "remoteok.com",
    "remotive.com", "himalayas.app", "arbeitnow.com", "jobicy.com", "rippling.com", "gem.com",
    "dover.com", "dover.io", "join.com", "pinpointhq.com", "jobvite.com", "icims.com", "myworkdayjobs.com",
    "substack.com", "tally.so", "hn.algolia.com", "loom.com", "apple.com", "discord.gg", "t.me",
}

URL_RE = re.compile(r"https?://[^\s\"'<>()]+", re.I)
ATS_RE = {
    "greenhouse": re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I),
}
ATS_SKIP_TOKENS = {"embed", "api", "v1", "jobs", "job_board"}


def extract_ats(text):
    """[(provider, token)] из текста (HTML-сущности уже раскодированы)."""
    out = []
    for prov, rx in ATS_RE.items():
        for m in rx.finditer(text):
            tok = urllib.parse.unquote(m.group(1)).strip(".").lower()
            if tok and tok not in ATS_SKIP_TOKENS:
                out.append((prov, tok))
    return list(dict.fromkeys(out))


def extract_domains(text):
    out = []
    for u in URL_RE.findall(text):
        host = urllib.parse.urlparse(u.rstrip(".,;")).netloc
        d = reg_domain(host)
        if d and "." in d and d not in NON_COMPANY_DOMAINS and not any(host.endswith(x) for x in NON_COMPANY_DOMAINS):
            out.append(d)
    return list(dict.fromkeys(out))


# =====================================================================
# Классификация вакансий: remote/регион, уровень, Go
# =====================================================================

US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
             "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_RE = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|us|america\b(?!s)|new york|nyc|san francisco|sf bay|bay area|seattle|"
    r"austin|boston|chicago|los angeles|denver|atlanta|miami|washington,? d\.?c|silicon valley|palo alto|"
    r"mountain view|menlo park|san jose|san diego|portland|philadelphia|pittsburgh|salt lake|"
    r"colorado springs|nashville|raleigh|dallas|houston|phoenix|minneapolis|detroit|"
    r"est|pst|cst|mst|edt|pdt)\b|,\s*(" + "|".join(US_STATES) + r")\b", re.I)
CA_RE = re.compile(r"\b(canada|toronto|vancouver|montreal|ottawa|calgary|waterloo|edmonton)\b|,\s*(ON|BC|QC|AB)\b", re.I)
RU_RE = re.compile(r"\b(russia|russian federation|moscow|saint[- ]petersburg|st\.? petersburg)\b|росси|москв", re.I)
GLOBAL_RE = re.compile(r"\b(worldwide|anywhere|global(ly)?|world|any (time ?zone|location|country)|international)\b", re.I)
EUROPE_RE = re.compile(
    r"\b(europe|european|emea|eu\b|eea|cet|cest|uk|united kingdom|england|london|germany|berlin|munich|"
    r"hamburg|france|paris|spain|madrid|barcelona|portugal|lisbon|netherlands|amsterdam|poland|warsaw|krakow|"
    r"ireland|dublin|sweden|stockholm|finland|helsinki|denmark|copenhagen|norway|oslo|switzerland|zurich|"
    r"austria|vienna|italy|milan|czech|prague|romania|bucharest|serbia|belgrade|estonia|tallinn|latvia|riga|"
    r"lithuania|vilnius|georgia\b(?! ?tech)|tbilisi|armenia|yerevan|cyprus|limassol|greece|athens|"
    r"bulgaria|sofia|hungary|budapest|croatia|slovenia|slovakia|belgium|brussels|luxembourg|turkey|istanbul)\b",
    re.I)
OTHER_RE = re.compile(
    r"\b(latam|latin america|south america|brazil|argentina|mexico|colombia|chile|india|singapore|"
    r"japan|tokyo|australia|sydney|new zealand|israel|tel aviv|uae|dubai|kazakhstan|almaty|astana|"
    r"uzbekistan|tashkent|kyrgyzstan|bishkek|middle east|africa|nigeria|kenya|egypt|south africa|"
    r"philippines|indonesia|malaysia|thailand|korea|taiwan|hong kong|pakistan)\b", re.I)
# Регион кандидата (Вьетнам): APAC/Asia целиком или сам Вьетнам/его часовой пояс
APAC_RE = re.compile(r"\b(apac|asia|asia[- ]pacific|south[- ]?east asia|sea region|vietnam|viet nam|ho chi minh|"
                     r"hanoi|da nang|gmt\s*\+\s*7|utc\s*\+\s*7)\b", re.I)
REMOTE_ALLOWED = {"global", "apac"}  # переопределяется флагом --remote-regions
REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh|fully[- ]remote|remote[- ]first)\b", re.I)
US_ONLY_TEXT_RE = re.compile(
    r"(must|need to|required to|should)\s+(be\s+)?(based|located|resid(e|ing)|living|live)\s+(in|within)\s+(the\s+)?"
    r"(us\b|u\.s\.|usa|united states|continental us)"
    r"|(authori[sz]ed|eligible|authori[sz]ation)\s+to\s+work\s+in\s+(the\s+)?(us\b|u\.s\.|usa|united states)"
    r"|\bu\.?s\.?\s+(work authori[sz]ation|citizen(ship)?|persons?)\b"
    r"|\bus[- ]based\s+(only|candidates|applicants)|remote\s*\(\s*us\s*\)|remote\s*[-–,]\s*us\b|\bus[- ]only\b"
    r"|(open|available)\s+(only\s+)?to\s+(candidates\s+)?(in|within|based in)\s+(the\s+)?(us\b|u\.s\.|united states)"
    r"|security clearance|\bclearance\b.*\b(secret|ts/sci)\b", re.I)
RELOC_RE = re.compile(r"relocation\s+(support|assistance|package|bonus|stipend|is (offered|provided|available))"
                      r"|help\s+(you\s+)?relocat|relocate\s+(you\s+)?to\s+\w+|релокац", re.I)
VISA_RE = re.compile(r"visa\s+sponsorship|sponsor\w*\s+(your\s+|a\s+|work\s+)?visas?|we\s+(can\s+|will\s+)?sponsor"
                     r"|blue card|work permit (support|assistance|sponsorship)", re.I)
NEG_RE = re.compile(r"\b(no|not|unable|cannot|can't|can not|don't|do not|won't|without|neither|nor)\b", re.I)
# отрицание ПОСЛЕ фразы: "Visa Sponsorship: Not available", "visa sponsorship is not provided", "...: No"
NEG_AFTER_RE = re.compile(r"^\W{0,5}(?:is\s+|are\s+)?(?:not\b|no\b|none\b|n/?a\b|unavailable|not\s+(?:available|"
                          r"provided|offered|possible))", re.I)
# Ограничения по региону/часовому поясу в заголовке или тексте: такие remote-вакансии из Вьетнама не подходят
RESTRICT_RE = re.compile(
    r"\b(latam|latin america|south america|central america)\b"
    r"|\b(eu|europe|emea|us|usa|u\.s\.|uk|canada|latam|americas?)[- ]only\b"
    r"|\bonly\s+(?:open\s+)?(?:to|in|within|for)\s+(?:candidates\s+)?(?:in\s+|from\s+|based in\s+)?(?:the\s+)?"
    r"(eu|europe|emea|us|usa|united states|uk|canada|latam|latin america|americas?)\b"
    r"|\b(pst|pdt|pt|est|edt|et|cst|cdt|mst|cet|cest|gmt|bst|eastern|pacific|central|mountain)\s*(?:time\s*)?"
    r"(?:zone\s+)?(?:\(?[^)]{0,20}\)?\s*)?(working|business|office|core)?\s*hours\b"
    r"|\boverlap(?:ping)?\s+(?:\w+\s+){0,4}(pst|pdt|est|edt|cst|cet|cest|us|u\.s\.|european|eu|pacific|eastern|"
    r"central|americas?|north american)\b"
    r"|\b(us|u\.s\.|north american|european|eu|cet|americas?)\s+(?:business\s+|working\s+)?(?:time\s*zones?|"
    r"hours)\b"
    r"|\b(?:within|in)\s+(?:the\s+)?(pst|est|cst|cet|us|european|americas?)\s+time\s*zones?\b"
    r"|remote\s*[(\[]\s*(us|usa|eu|uk|latam|europe|canada|emea)\s*[)\]]"
    r"|\b(?:utc|gmt)\s*[-−–]\s*[3-8]\b", re.I)
# хвост, который RemoteOK дописывает к каждому описанию
REMOTEOK_TAIL_RE = re.compile(r"\s*Please mention the word \*\*\w+\*\*.*$", re.S)

JUNIOR_RE = re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|early[- ]career|associate|"
                       r"apprentice|trainee)\b|\b(engineer|developer)\s+(i|1)\b\s*$", re.I)
MID_RE = re.compile(r"\b(mid|mid[- ]level|middle|intermediate)\b|\b(engineer|developer)\s+(ii|2)\b", re.I)
GO_TITLE_RE = re.compile(r"\bgolang\b|\bgo\b(?![- ]?to[- ]market)", re.I)
GO_TEXT_RE = re.compile(r"\bgolang\b|\bGo\b(?![- ]?(?:to|live|ahead|further|beyond|deep|getter|-getter|big|fast|after)\b)")
TECH_RE = re.compile(r"python|rust|java\b|kotlin|typescript|node|c\+\+|ruby|elixir|scala|grpc|kubernetes|k8s|"
                     r"postgres|microservice|backend|back-end|languages?|\bc#|php|swift|docker|aws|gcp|sql|"
                     r"redis|kafka|protobuf|terraform", re.I)
PY_RE = re.compile(r"\bpython\b|\bdjango\b|\bfastapi\b|\bflask\b", re.I)


def text_has_go(text):
    for m in GO_TEXT_RE.finditer(text):
        if m.group(0).lower() == "golang":
            return True
        window = text[max(0, m.start() - 80): m.end() + 80]
        if TECH_RE.search(window) or re.search(r"[(/,]\s*Go\b|\bGo\s*[,/)]|\bin Go\b|\bGo developer|\bGo engineer",
                                               window):
            return True
    return False


def region_tags(s):
    tags = set()
    if not s:
        return tags
    if GLOBAL_RE.search(s):
        tags.add("global")
    if US_RE.search(s):
        tags.add("us")
    if CA_RE.search(s):
        tags.add("ca")
    if RU_RE.search(s):
        tags.add("ru")
    if EUROPE_RE.search(s):
        tags.add("eu")
    if OTHER_RE.search(s):
        tags.add("other")
    if APAC_RE.search(s):
        tags.add("apac")
    if re.search(r"\bamericas\b|north america", s, re.I):
        tags.update({"us", "ca"} if "north america" in s.lower() else {"americas"})
    return tags


def _offer_flag(rx, text):
    """True - предлагают, False - явно отказывают, None - не сказано."""
    res = None
    for m in rx.finditer(text):
        before = text[max(0, m.start() - 60): m.start()]
        if NEG_RE.search(before) or NEG_AFTER_RE.search(text[m.end(): m.end() + 40]):
            res = False
        else:
            return True
    return res


def visa_flag(text):
    v = _offer_flag(VISA_RE, text)
    if v is None and re.search(r"(not|unable to|cannot|can't|don't|do not|won't)\s+(\w+\s+){0,3}sponsor", text, re.I):
        return False
    return v


def classify_job(j, blocked):
    """Добавляет к вакансии поля remote / regions / blocked_region / level / years / go / py / relocation."""
    locs = [j.get("location") or ""] + list(j.get("locations") or [])
    loc_str = " ; ".join(x for x in locs if x)
    text = j.get("text") or ""
    head = text[:3000]
    title = j.get("title") or ""
    remote =bool(j.get("remote")) or (j.get("workplace") or "").lower() == "remote" or bool(REMOTE_RE.search(loc_str))
    if (j.get("workplace") or "").lower() in ("hybrid", "onsite", "on-site") and not REMOTE_RE.search(loc_str):
        remote = False
    tags = region_tags(loc_str) | region_tags(j.get("country") or "")
    # регион в заголовке: "Backend Engineer (LATAM)", "Go Developer - EU"; часовые пояса отдельно ниже
    tags |= region_tags(re.sub(r"\b(est|pst|cst|mst|edt|pdt)\b", "", title, flags=re.I)) - {"global"}
    # явные ограничения в заголовке/тексте: LATAM, EU only, US only, "PST hours", "overlap with EST"...
    m = RESTRICT_RE.search(title) or RESTRICT_RE.search(text[:6000])
    restrict = re.sub(r"\s+", " ", m.group(0)).strip() if m else ""
    tz = j.get("tz") or []  # Himalayas: timezoneRestrictions (часы от UTC)
    if tz and not any(4 <= float(x) <= 10 for x in tz if isinstance(x, (int, float))):
        restrict = restrict or f"часовые пояса UTC{'/'.join(f'{x:+g}' for x in tz[:4])}"
    if j.get("country") and len(j["country"]) == 2:
        c = j["country"].upper()
        tags.add({"US": "us", "CA": "ca", "RU": "ru", "VN": "apac"}.get(c, "other"))
    us_text = bool(US_ONLY_TEXT_RE.search(text))
    if remote and not (tags - {"us", "ca"}) and us_text:
        tags.add("us")
    if remote and not tags and not us_text and GLOBAL_RE.search(head):
        tags.add("global")
    region_ok = not tags or bool(tags - blocked)
    if us_text and "us" in blocked and not (tags & {"global", "eu", "other", "apac"}):
        region_ok = False
    # remote подходит, только если разрешён регион кандидата: worldwide/anywhere/APAC/Asia;
    # "remote только для одной страны/региона" (US, India, Europe, LATAM...) - нет
    remote_region_ok = (not tags and not us_text) or bool(tags & REMOTE_ALLOWED)
    # "Worldwide, но работать по PST" из Вьетнама не подходит; явный APAC/Вьетнам перевешивает
    if restrict and "apac" not in (tags & REMOTE_ALLOWED):
        remote_region_ok = False
    visa = visa_flag(text)
    reloc = True if visa else _offer_flag(RELOC_RE, text)
    if visa is False and reloc:
        reloc = None  # "relocation assistance" без визы - это переезд внутри страны
    level = "junior" if JUNIOR_RE.search(title) else "mid" if MID_RE.search(title) else ""
    years = years_required(text)
    incomplete = bool(j.get("text_incomplete"))
    go = 2 if GO_TITLE_RE.search(title) else 1 if text_has_go(text) else (None if incomplete else 0)
    j.update({
        "remote": remote, "regions": sorted(tags), "region_ok": region_ok, "remote_region_ok": remote_region_ok,
        "restrict": restrict, "relocation": reloc, "visa": visa, "text_incomplete": incomplete,
        "ru": "ru" in tags and not (tags - {"ru"}),
        "level": level, "years": years, "go": go, "py": bool(PY_RE.search(title + " " + text[:8000])),
    })
    return j


# =====================================================================
# Провайдеры: нормализуем в {name, website, jobs:[{title, location, ...}]}
# =====================================================================

DESC_LIMIT = 20000  # символов описания в board_jobs.json


def board_text(raw, limit=DESC_LIMIT):
    """Описание вакансии для board_jobs.json: HTML (или plain) -> читаемый текст с абзацами и списками "- ",
    без тегов и битой кодировки, с нормализованными пробелами, не длиннее limit."""
    if not raw:
        return ""
    s = html.unescape(raw)
    if "&lt;" in s or "&amp;" in s:  # Greenhouse экранирует HTML дважды
        s = html.unescape(s)
    if "<" in s and ">" in s:
        s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
        s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"(?i)</(p|div|h[1-6]|ul|ol|section|blockquote|table|tr)>|<(p|h[1-6]|ul|ol|div)(\s[^>]*)?>", "\n\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = html.unescape(s)
        s = re.sub(r"\n-[ \t\xa0]*(?:\n[ \t\xa0]*)+", "\n- ", s)  # <li><p>текст</p></li>: маркер к тексту
    s = clean_text(s)
    lines, out = [l.strip() for l in s.split("\n")], []
    for l in lines:
        l = re.sub(r"^[•·▪●◦]\s*", "- ", l)
        if not l:
            if out and out[-1] != "":
                out.append("")
            continue
        # строки списка идут подряд, остальные строки - отдельные абзацы
        if l.startswith("- ") and len(out) >= 2 and out[-1] == "" and out[-2].startswith("- "):
            out.pop()
        if out and out[-1] != "" and not (l.startswith("- ") and out[-1].startswith("- ")):
            out.append("")
        out.append(l)
    s = "\n".join(out).strip()
    if len(s) > limit:
        cut = s[:limit]
        p = cut.rfind("\n\n")
        s = (cut[:p] if p > limit * 0.7 else cut.rsplit(" ", 1)[0]).rstrip() + "\n\n[…текст обрезан]"
    return s


def lever_text(j):
    """Lever: вступление, основное описание, списки (в них HTML) и дополнение - в один текст."""
    parts = [board_text(j.get("openingPlain") or ""),
             board_text(j.get("descriptionBodyPlain") or j.get("descriptionPlain") or "")]
    for x in j.get("lists") or []:
        body = board_text(x.get("content") or "")
        if body and not body.lstrip().startswith("- "):
            body = "- " + body
        parts.append(((x.get("text") or "").strip() + "\n\n" + body).strip())
    parts.append(board_text(j.get("additionalPlain") or ""))
    return board_text("\n\n".join(p for p in parts if p))


def _gh_jobs(token):
    d = HTTP.json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", timeout=40)
    if not isinstance(d, dict) or "jobs" not in d:
        raise NetError(f"greenhouse/{token}: неожиданный формат ответа")
    jobs = []
    for j in d["jobs"]:
        offices = [o.get("name") or "" for o in (j.get("offices") or [])]
        jobs.append({
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or {}).get("name", "") or "",
            "locations": offices,
            "remote": None,
            "url": j.get("absolute_url") or "",
            "text": strip_html(j.get("content", "")),
            "desc": board_text(j.get("content", "")),
            "company": j.get("company_name") or "",
            "posted": to_iso(j.get("first_published") or j.get("updated_at")),
            "updated": to_iso(j.get("updated_at")),
        })
    return jobs


def fetch_greenhouse(token, ident):
    jobs = _gh_jobs(token)
    if not ident:
        try:
            ident = {"name": (HTTP.json(f"https://boards-api.greenhouse.io/v1/boards/{token}") or {}).get("name", "")}
        except NotFound:
            ident = {"name": ""}
    if not ident.get("name"):
        ident["name"] = next((j["company"] for j in jobs if j["company"]), "")
    doms = {reg_domain(urllib.parse.urlparse(j["url"]).netloc) for j in jobs if j["url"]}
    doms -= {"greenhouse.io"}
    ident.setdefault("website", "")
    ident["job_domains"] = sorted(doms)
    return ident, jobs


def fetch_lever(token, ident):
    d = HTTP.json(f"https://api.lever.co/v0/postings/{token}?mode=json", timeout=40)
    if not isinstance(d, list):
        raise NetError(f"lever/{token}: неожиданный формат ответа")
    jobs = []
    for j in d:
        cat = j.get("categories") or {}
        lists = " ".join(f"{x.get('text', '')}: {strip_html(x.get('content', ''))}" for x in (j.get("lists") or []))
        text = " ".join(filter(None, [j.get("openingPlain"), j.get("descriptionBodyPlain") or j.get("descriptionPlain"),
                                      lists, j.get("additionalPlain")]))
        jobs.append({
            "title": (j.get("text") or "").strip(),
            "location": cat.get("location") or "",
            "locations": cat.get("allLocations") or [],
            "remote": (j.get("workplaceType") == "remote") or None,
            "workplace": j.get("workplaceType") or "",
            "country": j.get("country") or "",
            "url": j.get("hostedUrl") or "",
            "text": text,
            "desc": lever_text(j),
            "posted": to_iso(j.get("createdAt")),
        })
    if not ident:
        ident = {"name": "", "website": ""}
        try:
            page = HTTP.get(f"https://jobs.lever.co/{token}", accept="text/html")
            m = re.search(r"<title>([^<]*)</title>", page)
            ident["name"] = html.unescape(m.group(1)).strip() if m else ""
            m = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="main-header-logo"', page)
            ident["website"] = m.group(1) if m else ""
        except (NotFound, NetError):
            pass
    return ident, jobs


def fetch_ashby(token, ident):
    d = HTTP.json(f"https://api.ashbyhq.com/posting-api/job-board/{token}", timeout=40)
    if not isinstance(d, dict) or "jobs" not in d:
        raise NetError(f"ashby/{token}: неожиданный формат ответа")
    jobs = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        sec = [x.get("location") or "" for x in (j.get("secondaryLocations") or [])]
        addr = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
        jobs.append({
            "title": (j.get("title") or "").strip(),
            "location": j.get("location") or "",
            "locations": sec,
            "remote": bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote",
            "workplace": j.get("workplaceType") or "",
            "country": addr,
            "url": j.get("jobUrl") or "",
            "text": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or ""),
            "desc": board_text(j.get("descriptionHtml") or "") or board_text(j.get("descriptionPlain") or ""),
            "posted": to_iso(j.get("publishedAt")),
        })
    if not ident:
        ident = {"name": "", "website": ""}
        try:
            page = HTTP.get(f"https://jobs.ashbyhq.com/{token}", accept="text/html")
            m = re.search(r'"organization":\{"organizationId":"[^"]*","name":"((?:[^"\\]|\\.)*)","publicWebsite":'
                          r'(null|"((?:[^"\\]|\\.)*)")', page)
            if m:
                ident["name"] = json.loads(f'"{m.group(1)}"')
                ident["website"] = json.loads(f'"{m.group(3)}"') if m.group(3) else ""
        except (NotFound, NetError, ValueError):
            pass
    return ident, jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# =====================================================================
# Кандидаты
# =====================================================================

class Candidates:
    """Компании-кандидаты. Ключ - нормализованное название (или токен)."""

    def __init__(self):
        self.items = {}

    def add(self, name, source, direct=(), domains=(), hint="", job=None):
        name = re.sub(r"\s+", " ", html.unescape(name or "")).strip(" .,-|:")
        key = norm_name(name) or (direct[0][1] if direct else "")
        if not key or len(key) < 2:
            return None
        c = self.items.setdefault(key, {"key": key, "name": name, "sources": [], "direct": [], "domains": [],
                                        "hints": [], "jobs": []})
        if len(name) > len(c["name"]) and not c["direct"]:
            c["name"] = name
        if source not in c["sources"]:
            c["sources"].append(source)
        for d in direct:
            if list(d) not in c["direct"]:
                c["direct"].append(list(d))
        for d in domains:
            if d not in c["domains"]:
                c["domains"].append(d)
        if hint and len(c["hints"]) < 3:
            c["hints"].append(hint[:300])
        if job and not any(x.get("url") == job.get("url") for x in c["jobs"]):
            c["jobs"].append(job)
        return c

    def __len__(self):
        return len(self.items)


def looks_like_domain(s):
    return re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s.strip().lower()) is not None


def candidates_from_seed(path, cands):
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ats = extract_ats(line)
            if ats:
                cands.add(ats[0][1], "seed", direct=ats)
            elif looks_like_domain(line):
                cands.add(line.split(".")[0], "seed", domains=[reg_domain(line)])
            else:
                cands.add(line, "seed")
            n += 1
    log(f"seed: строк {n}")


# ---------- HN ----------

def find_hn_threads(months):
    """Последние темы 'Ask HN: Who is hiring?': Algolia, запасной путь - Firebase API HN."""
    ok = lambda t: (t or "").lower().startswith("ask hn: who is hiring")
    found = []
    q = urllib.parse.urlencode({"tags": "story,author_whoishiring", "hitsPerPage": 40})
    try:
        d = HTTP.json(f"https://hn.algolia.com/api/v1/search_by_date?{q}")
        found = [(h["objectID"], h["title"]) for h in d.get("hits", []) if ok(h.get("title"))]
    except (NotFound, NetError) as e:
        log(f"HN/Algolia недоступен ({e}); пробую Firebase API")
    if len(found) < months:
        try:
            u = HTTP.json("https://hacker-news.firebaseio.com/v0/user/whoishiring.json") or {}
            for i in (u.get("submitted") or [])[: 3 * months + 6]:
                it = HTTP.json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json") or {}
                if ok(it.get("title")) and str(i) not in {x[0] for x in found}:
                    found.append((str(i), it["title"]))
                if len(found) >= months:
                    break
        except (NotFound, NetError) as e:
            log(f"HN/Firebase недоступен ({e})")
    found.sort(key=lambda x: int(x[0]), reverse=True)
    return found[:months]


def hn_comments(thread_id, cache):
    """(заголовок темы, [HTML-тексты комментариев верхнего уровня])."""
    cached = cache.get(f"hn_{thread_id}", ttl_hours=6)
    if isinstance(cached, dict):
        return cached.get("title", ""), cached.get("texts", [])
    texts, title = [], ""
    try:
        item = HTTP.json(f"https://hn.algolia.com/api/v1/items/{thread_id}", timeout=90)
        title = item.get("title") or ""
        texts = [c.get("text") or "" for c in item.get("children", []) if c.get("text")]
    except (NotFound, NetError) as e:
        log(f"HN: Algolia items недоступен ({e}); читаю комментарии через Firebase (медленнее)")
        st = HTTP.json(f"https://hacker-news.firebaseio.com/v0/item/{thread_id}.json") or {}
        title = st.get("title") or ""
        kids = st.get("kids") or []

        def one(k):
            try:
                it = HTTP.json(f"https://hacker-news.firebaseio.com/v0/item/{k}.json") or {}
                return "" if it.get("deleted") or it.get("dead") else it.get("text") or ""
            except (NotFound, NetError):
                return ""
        with ThreadPoolExecutor(4) as ex:
            texts = [t for t in ex.map(one, kids) if t]
    if title and not title.lower().startswith("ask hn: who is hiring"):
        log(f"HN: внимание - тема {thread_id} называется '{title}', это не 'Who is hiring'")
    cache.put(f"hn_{thread_id}", {"title": title, "texts": texts})
    return title, texts


def hn_company_name(text):
    first = text.split("\n")[0]
    seg = re.split(r"\s+[|•]\s+|\s+[–—-]\s+", first, maxsplit=1)[0].strip()
    seg = re.sub(r"\(.*?\)", "", seg).strip(" ,.:")
    if looks_like_domain(seg):
        seg = seg.split(".")[0]
    if not seg or len(seg) > 50 or len(seg.split()) > 6:
        return ""
    return seg


def candidates_from_hn(cands, months, thread_ids, pattern, cache):
    threads = [(t, "") for t in thread_ids]
    if not threads:
        threads = find_hn_threads(months)
        if not threads:
            log("HN: тема 'Who is hiring' не найдена; укажите номер вручную: --hn-thread 50338728")
            return
    want = re.compile(pattern, re.I)
    for tid, title in threads:
        try:
            title2, raw = hn_comments(tid, cache)
            title = title or title2
        except (NotFound, NetError) as e:
            log(f"HN: тема {tid} не загружена: {e}")
            continue
        added = direct = 0
        for r in raw:
            un = html.unescape(r)
            un = re.sub(r'href="([^"]+)"', r" \1 ", un)
            text = strip_html(un.replace("<p>", "\n"))
            if not want.search(text):
                continue
            ats = extract_ats(un)
            name = hn_company_name(text)
            if not name and ats:
                name = ats[0][1]
            if not name:
                continue
            c = cands.add(name, f"hn:{tid}", direct=ats, domains=extract_domains(un)[:3],
                          hint=text.split("\n")[0])
            if c:
                added += 1
                direct += bool(ats)
        log(f"HN: тема {tid} {title} - комментариев {len(raw)}, подходящих компаний {added} "
            f"(с прямой ссылкой на ATS: {direct})")


# ---------- Публичные API вакансий ----------

def _src_job(source, company, title, url, location="", remote=None, tags=(), text="", level="", apply_url="",
             posted=None, tz=None, incomplete=False):
    return {"source": source, "company": (company or "").strip(), "title": (title or "").strip(), "url": url,
            "location": location or "", "remote": remote, "tags": [t for t in tags if t], "text": text,
            "level_hint": level, "apply_url": apply_url, "posted": to_iso(posted), "tz": list(tz or []),
            "text_incomplete": incomplete}


def remoteok_text(raw):
    """RemoteOK отдаёт в API только начало описания ("...") и дописывает служебный хвост.
    Возвращает (текст без хвоста, обрезан ли текст)."""
    t = REMOTEOK_TAIL_RE.sub("", strip_html(raw or "")).strip()
    t = re.sub(r"^Remote\s*-\s*", "", t)
    return t, bool(re.search(r"(\.\.\.|…)\s*$", t)) or len(t) < 400


def src_remoteok():
    d = HTTP.json("https://remoteok.com/api")
    out = []
    for j in d:
        if not (isinstance(j, dict) and j.get("position")):
            continue
        text, cut = remoteok_text(j.get("description"))
        out.append(_src_job("Remote OK", j.get("company"), j.get("position"), j.get("url"), j.get("location"), True,
                            j.get("tags") or [], text, apply_url=j.get("apply_url") or "",
                            posted=j.get("date") or j.get("epoch"), incomplete=cut))
    return out


def src_remotive():
    out = []
    for q in ("category=software-dev", "search=golang"):
        d = HTTP.json(f"https://remotive.com/api/remote-jobs?{q}")
        out += [_src_job("Remotive", j.get("company_name"), j.get("title"), j.get("url"),
                         j.get("candidate_required_location"), True, j.get("tags") or [],
                         strip_html(j.get("description")), posted=j.get("publication_date"))
                for j in d.get("jobs", [])]
    return out


def src_himalayas():
    out = []
    for q, pages in (("golang", 5), ("backend engineer", 3), ("python backend", 2)):
        for p in range(1, pages + 1):
            d = HTTP.json(f"https://himalayas.app/jobs/api/search?{urllib.parse.urlencode({'q': q, 'page': p})}")
            jobs = d.get("jobs") or []
            for j in jobs:
                out.append(_src_job("Himalayas", j.get("companyName"), j.get("title"),
                                    j.get("guid") or j.get("applicationLink"),
                                    ", ".join(j.get("locationRestrictions") or []) or "Worldwide", True,
                                    j.get("categories") or [], strip_html(j.get("description") or j.get("excerpt")),
                                    level=",".join(j.get("seniority") or []), posted=j.get("pubDate"),
                                    tz=j.get("timezoneRestrictions")))
            if len(jobs) < 20:
                break
    return out


def src_arbeitnow():
    out = []
    for p in range(1, 4):
        d = HTTP.json(f"https://www.arbeitnow.com/api/job-board-api?page={p}")
        for j in d.get("data", []):
            out.append(_src_job("Arbeitnow", j.get("company_name"), j.get("title"), j.get("url"), j.get("location"),
                                bool(j.get("remote")), j.get("tags") or [], strip_html(j.get("description")),
                                posted=j.get("created_at")))
        if not (d.get("links") or {}).get("next"):
            break
    return out


def src_jobicy():
    out = []
    for tag in ("golang", "backend", "python"):
        d = HTTP.json(f"https://jobicy.com/api/v2/remote-jobs?count=100&tag={tag}")
        for j in d.get("jobs", []):
            out.append(_src_job("Jobicy", j.get("companyName"), j.get("jobTitle"), j.get("url"), j.get("jobGeo"),
                                True, [], strip_html(j.get("jobDescription")), level=j.get("jobLevel") or "",
                                posted=j.get("pubDate")))
    return out


SOURCES = {"remoteok": src_remoteok, "remotive": src_remotive, "himalayas": src_himalayas,
           "arbeitnow": src_arbeitnow, "jobicy": src_jobicy}
SOURCE_TTL = {"remotive": 8}  # Remotive просит не чаще 4 раз в день


def candidates_from_sources(names, cands, a, cache, blocked):
    title_re, excl_re = re.compile(a.title, re.I), re.compile(a.exclude, re.I)
    stack_re = re.compile(r"golang|\bgo\b|backend|back-end|back end|python|django|fastapi", re.I)
    seen_urls = set()
    for name in names:
        ckey = f"src2_{name}"  # v2: с датой публикации и пометкой неполного текста
        jobs = cache.get(ckey, ttl_hours=max(a.source_ttl, SOURCE_TTL.get(name, 0)))
        if jobs is None:
            try:
                jobs = SOURCES[name]()
                cache.put(ckey, jobs)
            except (NotFound, NetError) as e:
                log(f"{name}: не загрузился ({e}); пропускаю")
                continue
        kept = 0
        for j in jobs:
            if j["url"] in seen_urls:  # одна вакансия приходит из нескольких запросов к источнику
                continue
            seen_urls.add(j["url"])
            t = j["title"]
            if not j["company"] or not title_re.search(t) or excl_re.search(t) or is_frontend_only(t):
                continue
            if re.search(r"senior|lead|principal|staff", j.get("level_hint") or "", re.I):
                continue
            if not stack_re.search(" ".join([t, " ".join(j["tags"]), j["text"][:4000]])):
                continue
            job = classify_job({"title": t, "location": j["location"], "remote": j["remote"], "text": j["text"],
                                "url": j["url"], "tz": j.get("tz"), "text_incomplete": j.get("text_incomplete")},
                               blocked)
            if not job_passes(job, a):
                continue
            job.update(source=j["source"], company=j["company"], tags=j["tags"][:10], posted=j.get("posted", ""))
            job["text"] = j["text"]
            ats = extract_ats(j.get("apply_url") or "") + extract_ats(j["text"][:20000])
            cands.add(j["company"], name, direct=ats[:2], job=job)
            kept += 1
        log(f"{name}: вакансий {len(jobs)}, подходящих {kept}")


# =====================================================================
# Оценка борда
# =====================================================================

def is_frontend_only(title):
    return bool(re.search(r"front[- ]?end|\bui\b|\bweb designer\b", title, re.I)) and \
        not re.search(r"back[- ]?end|full[- ]?stack|platform", title, re.I)


def job_passes(j, a):
    """Remote, доступный из региона кандидата (--remote-regions: worldwide/APAC), либо переезд с визой/релокацией.
    Remote только для одной страны/региона не подходит; офис в регионе из --block-regions (US/Canada) -
    только при спонсорстве визы; только Россия - никогда."""
    if a.max_years is not None and j["years"] is not None and j["years"] > a.max_years and j["level"] != "junior":
        return False
    if j.get("ru"):
        return False
    remote_ok = j["remote"] and j["remote_region_ok"]
    if j["region_ok"]:
        move_ok = bool(j["relocation"] or j["visa"])
    else:  # регион из блок-листа (US/CA): переезд реален только со спонсорством визы
        move_ok = bool(j["visa"])
    if a.remote_only:
        ok = remote_ok or move_ok
    else:
        ok = remote_ok or move_ok or (not j["remote"] and j["region_ok"])
    if not ok:
        return False
    if a.go_only and j["go"] == 0:  # None = текст неполный, Go неизвестен - не отсеиваем
        return False
    return True


def dedup_noats(jobs, stale_days):
    """Вакансии без борда: дубли по URL убираются; одинаковые компания+название с разными URL -
    это репост: остаётся самый свежий с пометкой reposts. Плюс age_days и stale для старых объявлений."""
    by_url = {}
    for j in jobs:
        by_url.setdefault(j["url"], j)
    by_pair = {}
    for j in by_url.values():
        k = (norm_name(j["company"]), re.sub(r"\W+", " ", j["title"].lower()).strip())
        by_pair.setdefault(k, []).append(j)
    out = []
    for group in by_pair.values():
        group.sort(key=lambda x: x.get("posted") or "", reverse=True)
        j = group[0]
        j["reposts"] = [{"url": x["url"], "posted": x.get("posted") or ""} for x in group[1:]]
        out.append(j)
    for j in out:
        mark_age(j, stale_days)
    out.sort(key=lambda j: (bool(j.get("stale")), -(j["go"] or 0), -(j["remote"] or 0), j["company"].lower()))
    return out


def mark_age(j, stale_days):
    d = age_days(j.get("posted")) if j.get("posted") else None
    j["age_days"] = None if d is None or d > 1e8 else int(d)
    j["stale"] = j["age_days"] is not None and j["age_days"] > stale_days
    return j


def verify(cand, provider, token, ident, jobs):
    """Насколько уверены, что борд принадлежит этой компании: direct/domain/name/text/mismatch."""
    if [provider, token] in cand["direct"]:
        return "direct"
    site = reg_domain(urllib.parse.urlparse(ident.get("website") or "").netloc)
    board_domains = ({site} if site else set()) | set(ident.get("job_domains") or [])
    if set(cand["domains"]) & board_domains:
        return "domain"
    bn = ident.get("name") or ""
    cn, bnn = norm_name(cand["name"]), norm_name(bn)
    cf, bf = full_norm(cand["name"]), full_norm(bn)
    if bnn and cn:
        if cn == bnn or cf == bf or cf == bnn or cn == bf:
            return "name"
        short, long_ = sorted((cn, bnn), key=len)
        if len(short) >= 5 and long_.startswith(short) and len(long_) - len(short) <= 6:
            return "name"
        # 'Nirvana' в HN и борд 'Nirvana Insurance' - вероятно та же компания (сокращённое имя);
        # обратный случай ('Sphinx Defense' -> борд 'Sphinx') - почти всегда чужая компания
        if len(cn) >= 5 and bnn.startswith(cn):
            return "weak"
        if cand["domains"] and site and site.split(".")[0] in (cn, cf):
            return "domain"
        return "mismatch"
    # имени борда нет: ищем название компании в тексте вакансий
    nm = cand["name"]
    if len(nm) >= 4:
        rx = re.compile(r"\b" + re.escape(nm) + r"\b", re.I)
        if sum(bool(rx.search(j["text"][:5000])) for j in jobs[:10]) >= 1:
            return "text"
    return "mismatch"


JOB_KEYS = ("title", "location", "url", "remote", "regions", "go", "py", "level", "years", "relocation", "visa",
            "posted", "restrict", "text_incomplete", "age_days", "stale")
CONF_SCORE = {"direct": 3, "domain": 3, "name": 2, "weak": 0, "text": 0}


def evaluate(cand, provider, token, ident, jobs, a, blocked, conf):
    title_re, excl_re = re.compile(a.title, re.I), re.compile(a.exclude, re.I)
    matching = []
    stack_go = sum(1 for j in jobs if GO_TITLE_RE.search(j["title"]) or text_has_go(j["text"][:8000]))
    for j in jobs:
        t = j["title"]
        if not title_re.search(t) or excl_re.search(t) or is_frontend_only(t):
            continue
        j = classify_job(dict(j), blocked)
        if not job_passes(j, a):
            continue
        matching.append(mark_age(j, a.stale_days))
    go = sum(1 for j in matching if j["go"])
    go_title = sum(1 for j in matching if j["go"] == 2)
    remote_ok = sum(1 for j in matching if j["remote"] and j["remote_region_ok"])
    reloc = sum(1 for j in matching if j["relocation"])
    leveled = sum(1 for j in matching if j["level"])
    score = (3 * go_title + 2 * go + 2 * min(remote_ok, 3) + min(reloc, 2) + min(len(matching), 4)
             + leveled + CONF_SCORE.get(conf, 0) - len(jobs) / 25)
    if a.prefer_python:
        score += min(sum(1 for j in matching if j["py"]), 3)
    company = ident.get("name") or cand["name"]
    if matching:
        keep = matching
    else:  # "наблюдение: Go в стеке" - инженерные вакансии с Go (фильтр названий, но без уровня/региона)
        keep = [mark_age(classify_job(dict(j), blocked), a.stale_days) for j in jobs
                if (GO_TITLE_RE.search(j["title"]) or text_has_go(j["text"][:8000]))
                and title_re.search(j["title"]) and not excl_re.search(j["title"])
                and not is_frontend_only(j["title"])][:10]
    board_jobs = [board_job_record(j, provider, token, company, conf, watch=not matching) for j in keep]
    return {
        "provider": provider, "token": token, "url": BOARD_URL[provider].format(t=token),
        "company": ident.get("name") or cand["name"], "candidate": cand["name"], "website": ident.get("website") or "",
        "sources": cand["sources"], "confidence": conf, "total_jobs": len(jobs), "matching": len(matching),
        "go": go, "remote": remote_ok, "relocation": reloc, "score": round(score, 2), "stack_go": stack_go,
        "watch": not matching,
        "jobs": [dict({k: j.get(k) for k in JOB_KEYS}, _text=(j.get("text") or "")[:4000]) for j in matching[:8]],
        "_board_jobs": board_jobs,  # -> board_jobs.json, в boards.json не пишется
    }


def board_job_record(j, provider, token, company, conf, watch):
    """Запись board_jobs.json: формат jobs_noats.json (text, posted, regions, go, py ...) плюс полное описание."""
    desc = j.get("desc") or board_text(j.get("text") or "")
    rec = {k: j.get(k) for k in JOB_KEYS}
    rec.update({
        "company": company, "source": provider, "provider": provider, "board_token": token,
        "board_url": BOARD_URL[provider].format(t=token), "confidence": conf, "watch": watch,
        "description": desc, "text": desc,
        "published_at": j.get("posted") or "", "updated_at": j.get("updated") or "",
        "go": j.get("go") or 0, "py": bool(j.get("py")),
    })
    return rec


def probe_candidate(cand, a, state, blocked, blacklist):
    """Ищет борды кандидата. Возвращает (results, probe_updates, errors)."""
    results, updates, errors = [], {}, 0
    plan = []  # (provider, token)
    for prov, tok in cand["direct"]:
        plan.append((prov, tok))
    direct_provs = {p for p, _ in cand["direct"]}
    if not a.direct_only or not cand["direct"]:
        for tok in slug_variants(cand["name"]):
            for prov in PROVIDERS:
                if prov not in direct_provs:
                    plan.append((prov, tok))
    done_provs = set()
    for prov, tok in dict.fromkeys(plan):
        if prov in done_provs:
            continue
        key = f"{prov}:{tok}"
        prev = state["probes"].get(key) or {}
        is_direct = [prov, tok] in cand["direct"]
        if prev.get("status") in ("none", "big") and age_days(prev.get("at")) < a.recheck_days and not a.refresh:
            continue
        if prev.get("status") == "mismatch" and prev.get("for") == cand["key"] and not is_direct \
                and age_days(prev.get("at")) < a.recheck_days and not a.refresh:
            continue
        ident = None
        if prev.get("status") == "found" and prev.get("name") is not None and age_days(prev.get("ident_at")) < 30:
            ident = {"name": prev.get("name"), "website": prev.get("website", "")}
        try:
            ident, jobs = FETCHERS[prov](tok, ident)
        except NotFound:
            updates[key] = {"status": "none", "at": now_iso()}
            continue
        except NetError:
            errors += 1
            continue
        if not jobs and not ident.get("name"):
            updates[key] = {"status": "none", "at": now_iso()}
            continue
        conf = verify(cand, prov, tok, ident, jobs)
        base = {"at": now_iso(), "name": ident.get("name", ""), "website": ident.get("website", ""),
                "ident_at": prev.get("ident_at") if prev.get("name") is not None and prev.get("status") == "found"
                and age_days(prev.get("ident_at")) < 30 else now_iso()}
        if conf == "mismatch":
            updates[key] = dict(base, status="mismatch", **{"for": cand["key"]})
            continue
        done_provs.add(prov)
        if len(jobs) > a.max_jobs:
            updates[key] = dict(base, status="big", total=len(jobs))
            continue
        bl = {norm_name(ident.get("name") or ""), tok, cand["key"]} & blacklist
        if bl:
            updates[key] = dict(base, status="found", blacklisted=True)
            continue
        updates[key] = dict(base, status="found", total=len(jobs))
        r = evaluate(cand, prov, tok, ident, jobs, a, blocked, conf)
        if r["matching"] or (a.watchlist and r["stack_go"] and conf != "weak"):
            results.append(r)
    return results, updates, errors


# =====================================================================
# Состояние и кэш
# =====================================================================

def load_state(path):
    s = load_json(path, {})
    s.setdefault("version", 2)
    for k in ("probes", "boards", "companies"):
        s.setdefault(k, {})
    return s


def save_state(path, s):
    save_json(path, s, indent=1, sort_keys=True)


def load_blacklist(path):
    out = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if line:
                    ats = extract_ats(line)
                    out.add(ats[0][1] if ats else norm_name(line))
    return out


# =====================================================================
# Вывод
# =====================================================================

def fmt_date(j):
    if not j.get("posted"):
        return "дата ?"
    s = f"{j['posted'][8:10]}.{j['posted'][5:7]}.{j['posted'][:4]}"
    if j.get("stale"):
        s += f" ⚠️ старое ({j['age_days']} дн.)"
    return s


def fmt_flags(j):
    f = []
    if j.get("go") == 2:
        f.append("Go в названии")
    elif j.get("go"):
        f.append("Go")
    elif j.get("go") is None:
        f.append("Go: ?")
    if j.get("text_incomplete"):
        f.append("⚠️ текст неполный, проверить вручную")
    if j.get("py"):
        f.append("Python")
    if j.get("remote"):
        f.append("remote" + (f" ({', '.join(j['regions'])})" if j.get("regions") else ""))
    if j.get("visa"):
        f.append("спонсорство визы")
    elif j.get("relocation"):
        f.append("релокация")
    if j.get("level"):
        f.append(j["level"])
    if j.get("years"):
        f.append(f"{j['years']}+ лет")
    return ", ".join(f)


def md_escape(s):
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def run_ai(found, noats, a):
    """AI-оценка вакансий бордов и вакансий без борда. Сначала Go и remote - чтобы лимит ушёл на главное.
    Борды и список без борда пересортировываются по AI-оценке."""
    pool = []
    for r in found:
        for j in r["jobs"]:
            j.update(company=r["company"], source=r["provider"], text=j.get("_text") or "")
            pool.append(j)
    pool += noats
    pool.sort(key=lambda j: (-(j.get("go") or 0), not j.get("remote"), j.get("stale", False)))
    AI.score_jobs(HTTP, pool, limit=a.ai_limit)
    for r in found:
        for j in r["jobs"]:
            j.pop("text", None)
            j.pop("company", None)
            j.pop("source", None)
        sc = [j["ai"]["score"] for j in r["jobs"] if j.get("ai")]
        r["ai_max"] = max(sc) if sc else None
        r["jobs"].sort(key=lambda j: -(j.get("ai") or {}).get("score", -1))
    found.sort(key=lambda r: (r["watch"], -(r["ai_max"] if r["ai_max"] is not None else -1), -r["score"]))
    noats.sort(key=lambda j: (bool(j.get("stale")), -(j.get("ai") or {}).get("score", -1), -(j["go"] or 0)))


def write_outputs(found, noats, a, stats, run_at):
    os.makedirs(a.out_dir, exist_ok=True)
    P = lambda name: os.path.join(a.out_dir, name)
    board_jobs = []
    for r in found:  # текст вакансий нужен был только для AI-оценки
        ai = {j["url"]: j["ai"] for j in r["jobs"] if j.get("ai")}
        for j in r["jobs"]:
            j.pop("_text", None)
        for bj in r.pop("_board_jobs", []):
            if bj["url"] in ai:
                bj["ai"] = ai[bj["url"]]
            board_jobs.append(bj)
    seen_urls = set()
    board_jobs = [b for b in board_jobs if b["url"] and not (b["url"] in seen_urls or seen_urls.add(b["url"]))]
    with open(P("board_jobs.json"), "w", encoding="utf-8") as f:
        json.dump(board_jobs, f, ensure_ascii=False, indent=2)
    with open(P("boards.json"), "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)
    with open(P("jobs_noats.json"), "w", encoding="utf-8") as f:
        json.dump(noats, f, ensure_ascii=False, indent=2)

    per = {}
    for prov in PROVIDERS:
        rows = [r for r in found if r["provider"] == prov]
        per[prov] = rows
        for i in range(max(1, a.chunks)):
            chunk = rows[i * JOBSYNC_LIMIT:(i + 1) * JOBSYNC_LIMIT]
            fname = P(f"boards_{prov}.txt" if i == 0 else f"boards_{prov}_{i + 1}.txt")
            if i > 0 and not chunk:
                break
            with open(fname, "w", encoding="utf-8") as f:
                f.write("".join(r["url"] + "\n" for r in chunk))

    L = [f"# Борды для JobSync - {run_at[:16].replace('T', ' ')} UTC", ""]
    L.append(f"Кандидатов: {stats['candidates']}, проверено бордов-вариантов: {stats['probed']}, "
             f"найдено подходящих бордов: {len(found)} (новых: {sum(r['new'] for r in found)}), "
             f"ошибок сети: {stats['errors']}.")
    L.append(f"Фильтры: remote-only={a.remote_only}, max-jobs={a.max_jobs}, max-years={a.max_years}, "
             f"remote из регионов: {a.remote_regions}, офис только с визой: {a.block_regions or '-'}.")
    L.append("")
    L.append("Уверенность: **direct** - ссылка на борд была в источнике; **domain** - совпал домен сайта; "
             "**name** - совпало название; **weak** - название борда длиннее (напр. 'Nirvana' -> 'Nirvana Insurance'), "
             "**text** - название найдено только в тексте вакансий: эти два проверьте вручную.")
    L += ["", "## Провайдеры в этом запуске", "",
          "| Провайдер | Прямых ссылок у кандидатов | Борд существует | из них крупные (>max-jobs) | в чёрном списке | "
          "чужой борд | Нет борда | С подходящими вакансиями |", "|---|---|---|---|---|---|---|---|"]
    for prov in PROVIDERS:
        ps = stats["providers"][prov]
        exist = ps.get("found", 0) + ps.get("big", 0) + ps.get("blacklisted", 0)
        L.append(f"| {prov} | {ps.get('direct', 0)} | {exist} | {ps.get('big', 0)} | {ps.get('blacklisted', 0)} | "
                 f"{ps.get('mismatch', 0)} | {ps.get('none', 0)} | {len(per[prov])} |")
    L.append("")
    L.append("Токены, проверенные в прошлые запуски и не найденные, повторно не запрашиваются "
             f"{a.recheck_days} дн. (--refresh - проверить заново). Если у провайдера 0 подходящих, но борды "
             "существуют - провайдер работает, просто у этих компаний сейчас нет вакансий под фильтры.")
    for prov in PROVIDERS:
        rows = per[prov]
        L += ["", f"## {prov.capitalize()}: {len(rows)} (в файл boards_{prov}.txt попали первые "
                  f"{min(len(rows), JOBSYNC_LIMIT)})", ""]
        if not rows:
            L.append("_ничего не найдено_")
            continue
        L.append("| # | Компания | Новая | Увер. | Вакансий | Подходит | Go | Remote | Релок. | AI | Борд |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rows, 1):
            nm = md_escape(r['company']) + (" _(наблюдение: Go в стеке)_" if r.get("watch") else "")
            L.append(f"| {i} | {nm} | {'да' if r['new'] else ''} | {r['confidence']} | "
                     f"{r['total_jobs']} | {r['matching']} | {r['go']} | {r['remote']} | {r['relocation']} | "
                     f"{r.get('ai_max') if r.get('ai_max') is not None else ''} | [{r['token']}]({r['url']}) |")
    L += ["", "## Подходящие вакансии по компаниям", ""]
    for r in found:
        L.append(f"### {md_escape(r['company'])} - {r['provider']} ({r['confidence']}"
                 f"{', новая' if r['new'] else ''})")
        L.append(f"Борд: {r['url']}" + (f" · сайт: {r['website']}" if r["website"] else "")
                 + f" · источники: {', '.join(r['sources'])}")
        if r.get("watch"):
            L.append(f"- подходящих вакансий сейчас нет; Go упоминается в {r['stack_go']} из {r['total_jobs']} вакансий")
        for j in r["jobs"]:
            L.append(f"- [{md_escape(j['title'])}]({j['url']}) - {md_escape(j['location']) or '?'} - {fmt_date(j)}"
                     + (f" - {fmt_flags(j)}" if fmt_flags(j) else ""))
            if j.get("ai"):
                L.append(f"  - {md_escape(AI.fmt_ai(j['ai']))}")
        L.append("")
    if noats:
        L += ["## Вакансии без борда Greenhouse/Lever/Ashby (из открытых API)", "",
              "Добавить в JobSync можно через `python3 common/jobsync_push.py` (см. --help). "
              "Источник указан у каждой вакансии - так требуют условия API.", ""]
        for j in noats[:80]:
            rep = f" - репост ({len(j['reposts'])} раз)" if j.get("reposts") else ""
            L.append(f"- **{md_escape(j['company'])}** - [{md_escape(j['title'])}]({j['url']}) - "
                     f"{md_escape(j['location']) or '?'} - {fmt_date(j)}{rep} - {fmt_flags(j)} - источник: {j['source']}")
            if j.get("ai"):
                L.append(f"  - {md_escape(AI.fmt_ai(j['ai']))}")
        if len(noats) > 80:
            L.append(f"- ... и ещё {len(noats) - 80} в jobs_noats.json")
    with open(P(a.report), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return per


# =====================================================================
# main
# =====================================================================

class RuHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, *args, **kw):
        kw.setdefault("max_help_position", 32)
        super().__init__(*args, **kw)

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix or "использование: ")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=RuHelpFormatter, add_help=False)
    p._positionals.title = "позиционные аргументы"
    p._optionals.title = "параметры"
    p.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")
    g = p.add_argument_group("источники кандидатов")
    g.add_argument("--hn", action="store_true", help="последняя тема HN 'Who is hiring'")
    g.add_argument("--hn-months", type=int, default=0, metavar="N",
                   help="взять N последних тем HN (включает --hn); по умолчанию 1")
    g.add_argument("--hn-thread", action="append", default=[], metavar="ID",
                   help="номер темы HN вручную (можно несколько раз), напр. 50338728")
    g.add_argument("--hn-filter", default=r"golang|(?-i:\bGo\b)|backend|back-end|python",
                   help="regex по тексту HN-комментария (по умолчанию: Go/backend/Python)")
    g.add_argument("--sources", default="",
                   help="публичные API через запятую: remoteok,remotive,himalayas,arbeitnow,jobicy или all")
    g.add_argument("--seed", action="append", default=[], metavar="FILE",
                   help="файл: названия компаний, домены, токены или ссылки на борды (по одному в строке)")
    g = p.add_argument_group("фильтры")
    g.add_argument("--title", default=r"backend|back-end|back end|golang|\bgo\b|software (engineer|developer)|"
                                      r"platform (engineer|developer)|infrastructure engineer|\bapi\b|distributed systems|full[- ]?stack|python|"
                                      r"server[- ]side|(go|golang|python|backend) developer",
                   help="regex: какие названия вакансий подходят")
    g.add_argument("--exclude", default=r"senior|\bsr\.?\b|staff|principal|\blead\b|tech lead|head of|director|"
                                        r"manager|architect|intern\b|internship|\bvp\b|vice president|chief|"
                                        r"\biii\b|\biv\b|\bl[5-9]\b|ios|android|mobile|embedded|firmware|hardware|"
                                        r"machine learning|\bml\b|data scien|research|sales|solutions engineer|"
                                        r"support engineer|designer|\bqa\b|\bsdet\b|test|recruit|forward deployed|"
                                        r"founding|expert|specialist|\bphd\b|analyst|product owner|consultant|account|"
                                        r"devrel|developer relations|advocate|\bit support",
                   help="regex: какие названия исключить (senior/lead/... и не-backend роли)")
    g.add_argument("--max-years", type=int, default=DEFAULT_MAX_YEARS, metavar="N",
                   help=f"отсеивать вакансии, где требуют больше N лет опыта (по умолчанию {DEFAULT_MAX_YEARS}; "
                        f"-1 = не проверять)")
    g.add_argument("--max-jobs", type=int, default=50,
                   help="компания 'маленькая', если открытых вакансий не больше (по умолчанию 50)")
    g.add_argument("--remote-only", action="store_true",
                   help="только remote-вакансии, доступные из --remote-regions, или с релокацией/визой")
    g.add_argument("--remote-regions", default="global,apac",
                   help="для remote: какие ограничения региона подходят. global = worldwide/anywhere, apac = "
                        "APAC/Asia/Вьетнам (по умолчанию global,apac). Ещё: eu, us, ca, americas, other")
    g.add_argument("--block-regions", default="us,ca,ru",
                   help="для офиса: регионы, куда переезд реален только со спонсорством визы (us,ca,ru)")
    g.add_argument("--go-only", action="store_true", help="только вакансии с упоминанием Go")
    g.add_argument("--stale-days", type=int, default=30, metavar="N",
                   help="помечать как старые объявления, опубликованные больше N дней назад (30)")
    g.add_argument("--prefer-python", action="store_true", help="поднимать в рейтинге вакансии с Python")
    g.add_argument("--ai", action="store_true",
                   help="AI-оценка подходящих вакансий под резюме (DeepSeek: DEEPSEEK_API_KEY, resume.txt в корне; "
                        "общий модуль common/ai_score.py, кэш state/ai_scores.json)")
    g.add_argument("--ai-limit", type=int, default=40, metavar="N",
                   help="максимум платных AI-запросов за запуск (40); оценки из кэша бесплатны")
    g.add_argument("--blacklist", default=os.path.join(HERE, "blacklist.txt"),
                   help="файл с крупными/неинтересными компаниями (по умолчанию boards/blacklist.txt)")
    g.add_argument("--watchlist", action="store_true",
                   help="добавлять в конец списков небольшие компании со стеком Go, даже если сейчас "
                        "подходящих вакансий нет (JobSync поймает будущие)")
    g.add_argument("--direct-only", action="store_true",
                   help="для кандидатов с прямой ссылкой не угадывать токены у других провайдеров")
    g = p.add_argument_group("вывод и состояние")
    g.add_argument("--out-dir", default=os.path.join(HERE, "out"), help="папка для результатов (по умолчанию boards/out)")
    g.add_argument("--report", default="report.md", help="имя файла отчёта")
    g.add_argument("--chunks", type=int, default=1,
                   help="сколько файлов по 25 ссылок на провайдера (boards_X.txt, boards_X_2.txt, ...)")
    g.add_argument("--state", default=os.path.join(STATE_DIR, "seen_boards.json"),
                   help="файл состояния между запусками (по умолчанию state/seen_boards.json)")
    g.add_argument("--cache-dir", default=os.path.join(STATE_DIR, "cache", "boards"),
                   help="кэш ответов HN и источников (по умолчанию state/cache/boards)")
    g.add_argument("--recheck-days", type=int, default=21,
                   help="через сколько дней перепроверять токены без борда / чужие борды (по умолчанию 21)")
    g.add_argument("--refresh", action="store_true", help="игнорировать кэш 'борда нет' и проверить всё заново")
    g.add_argument("--source-ttl", type=float, default=6, help="сколько часов держать кэш источников (6)")
    g.add_argument("--no-cache", action="store_true", help="не использовать кэш ответов HN и источников")
    g = p.add_argument_group("сеть")
    g.add_argument("--workers", type=int, default=4, help="параллельных проверок (по умолчанию 4)")
    g.add_argument("--retries", type=int, default=3, help="повторов при сетевых ошибках (3)")
    g.add_argument("--interval", type=float, default=0.25,
                   help="мин. пауза между запросами к одному API, сек (0.25)")
    g.add_argument("--insecure", action="store_true", help="не проверять SSL-сертификаты (крайний случай)")
    g.add_argument("--limit", type=int, default=0, help="проверить не больше N кандидатов (для отладки)")
    return p


def main(argv=None):
    global HTTP
    p = build_parser()
    a = p.parse_args(argv)
    if a.max_years is not None and a.max_years < 0:
        a.max_years = None
    if a.hn_months:
        a.hn = True
    months = max(1, a.hn_months or 1)
    try:
        re.compile(a.title), re.compile(a.exclude), re.compile(a.hn_filter, re.I)
    except re.error as e:
        p.error(f"неверный regex: {e}")
    srcs = [s.strip().lower() for s in a.sources.split(",") if s.strip()]
    if srcs == ["all"]:
        srcs = list(SOURCES)
    bad = [s for s in srcs if s not in SOURCES]
    if bad:
        p.error(f"неизвестные источники: {', '.join(bad)} (доступны: {', '.join(SOURCES)})")
    if not (a.hn or a.hn_thread or a.seed or srcs):
        p.error("нет источников кандидатов: укажите --hn, --hn-thread, --sources и/или --seed")

    HTTP = Http(UA, intervals=HTTP_INTERVALS, default_interval=a.interval, retries=a.retries, timeout=20,
                insecure=a.insecure, backoff=1.5, retry_after_cap=90, report_errors=True)
    blocked = {x.strip().lower() for x in a.block_regions.split(",") if x.strip()}
    REMOTE_ALLOWED.clear()
    REMOTE_ALLOWED.update(x.strip().lower() for x in a.remote_regions.split(",") if x.strip())
    os.makedirs(a.out_dir, exist_ok=True)
    state_path = a.state
    state = load_state(state_path)
    cache = Cache(a.cache_dir, disabled=a.no_cache)
    blacklist = load_blacklist(a.blacklist)
    if blacklist:
        log(f"чёрный список: {len(blacklist)} компаний")
    run_at = now_iso()

    cands = Candidates()
    for s in a.seed:
        candidates_from_seed(s, cands)
    if a.hn or a.hn_thread:
        candidates_from_hn(cands, months, a.hn_thread, a.hn_filter, cache)
    if srcs:
        candidates_from_sources(srcs, cands, a, cache, blocked)
    items = [c for c in cands.items.values() if c["key"] not in blacklist]
    if a.limit:
        items = items[: a.limit]
    if not items:
        log("Кандидатов нет - проверьте источники и сообщения выше.")
        return 1
    for c in items:
        rec = state["companies"].setdefault(c["key"], {"name": c["name"], "first_seen": run_at})
        rec["last_seen"] = run_at
        rec["sources"] = sorted(set(rec.get("sources", [])) | set(c["sources"]))
    log(f"Кандидатов: {len(items)} (с прямой ссылкой на борд: {sum(bool(c['direct']) for c in items)}). Проверяю...")

    found, errors, probed = [], 0, 0
    prov_stats = {p: {} for p in PROVIDERS}
    for c in items:
        for prov, _ in c["direct"]:
            prov_stats[prov]["direct"] = prov_stats[prov].get("direct", 0) + 1
    board_cands = set()
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = {ex.submit(probe_candidate, c, a, state, blocked, blacklist): c for c in items}
            for i, f in enumerate(as_completed(futs), 1):
                c = futs[f]
                try:
                    res, upd, err = f.result()
                except Exception as e:  # ошибка в одном кандидате не должна ронять весь прогон
                    log(f"  {c['name']}: внутренняя ошибка {type(e).__name__}: {e}")
                    errors += 1
                    continue
                state["probes"].update(upd)
                probed += len(upd)
                for k, u in upd.items():
                    ps = prov_stats[k.split(":", 1)[0]]
                    st = "blacklisted" if u.get("blacklisted") else u.get("status")
                    ps[st] = ps.get(st, 0) + 1
                errors += err
                if any(u.get("status") == "found" for u in upd.values()) or res:
                    board_cands.add(c["key"])
                found.extend(res)
                if i % 25 == 0 or i == len(futs):
                    log(f"  {i}/{len(futs)} кандидатов, бордов с подходящими вакансиями: {len(found)}, "
                        f"запросов: {HTTP.requests}, {time.time() - t0:.0f}с")
                if i % 100 == 0:
                    save_state(state_path, state)
    except KeyboardInterrupt:
        log("Прервано - сохраняю то, что успел найти.")

    # убираем дубли (один борд у нескольких кандидатов) - оставляем лучший
    best = {}
    for r in found:
        k = (r["provider"], r["token"])
        if k not in best or CONF_SCORE.get(r["confidence"], 0) > CONF_SCORE.get(best[k]["confidence"], 0):
            best[k] = r
    found = list(best.values())
    for r in found:
        k = f"{r['provider']}:{r['token']}"
        rec = state["boards"].setdefault(k, {"first_seen": run_at, "company": r["company"]})
        rec["last_seen"] = run_at
        rec["matching"] = r["matching"]
        r["new"] = rec["first_seen"] == run_at
    found.sort(key=lambda r: (r["watch"], -r["score"], -r["go"], -r["remote"], r["total_jobs"]))

    noats = dedup_noats([dict({k: j.get(k) for k in JOB_KEYS + ("company", "source", "tags", "text")})
                         for c in items if c["key"] not in board_cands for j in c["jobs"]], a.stale_days)

    if a.ai:
        run_ai(found, noats, a)

    save_state(state_path, state)
    stats = {"candidates": len(items), "probed": probed, "errors": errors, "providers": prov_stats}
    per = write_outputs(found, noats, a, stats, run_at)

    for prov in PROVIDERS:
        rows = per[prov][:JOBSYNC_LIMIT]
        print(f"\n=== {prov}: {len(per[prov])} найдено, в boards_{prov}.txt - {len(rows)} ===")
        for r in rows:
            print(f"{'*' if r['new'] else ' '} {r['company'][:28]:<28} {r['confidence']:<6} всего {r['total_jobs']:>3} | "
                  f"подходит {r['matching']} | Go {r['go']} | remote {r['remote']} | релок. {r['relocation']}"
                  + (f" | наблюдение (Go в стеке: {r['stack_go']})" if r.get("watch") else ""))
            for j in r["jobs"][:2]:
                print(f"     - {j['title']} ({j['location'] or '?'})")
    for prov in PROVIDERS:
        ps = prov_stats[prov]
        exist = ps.get("found", 0) + ps.get("big", 0) + ps.get("blacklisted", 0)
        log(f"{prov}: прямых ссылок {ps.get('direct', 0)}, бордов существует {exist} (крупных {ps.get('big', 0)}, "
            f"в чёрном списке {ps.get('blacklisted', 0)}), чужих {ps.get('mismatch', 0)}, нет борда {ps.get('none', 0)}, "
            f"с подходящими вакансиями {len(per[prov])}")
    print(f"\n* - новая компания. Отчёт: {os.path.join(a.out_dir, a.report)}; "
          f"вакансий без ATS: {len(noats)} (jobs_noats.json)")
    log(f"Готово за {time.time() - t0:.0f}с: запросов {HTTP.requests}, сетевых ошибок {errors}.")
    if errors:
        log("Часть запросов не удалась - эти борды не попали в кэш 'нет борда'; "
            "просто запустите скрипт ещё раз, он проверит только их.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
