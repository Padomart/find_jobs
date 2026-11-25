"""
AI-оценка вакансий под резюме кандидата - общая для en/ и ru/.

Провайдер - любой OpenAI-совместимый chat/completions (по умолчанию DeepSeek). Ключ только из
переменной окружения DEEPSEEK_API_KEY (или AI_API_KEY). Резюме - короткий текст в resume.txt
в корне проекта (в .gitignore; путь можно поменять).

Платные запросы идут только по тем вакансиям, которые передал вызывающий (обычно - прошедшие
фильтры), не больше limit за запуск. Оценки кэшируются в state/ai_scores.json по URL+названию,
поэтому повторный запуск за ту же вакансию не платит.

Понимает оба формата вакансий: boards (text, location, remote, regions) и ru (description, salary).
Результат кладётся в job["ai"] = {score 0-10, level, remote, go_stack, reason, red_flags}.
"""
import hashlib
import json
import os
import re
import sys

from common import ROOT, STATE_DIR
from common.httpclient import HttpError, NetError
from common.state import load_json, save_json

DEFAULT_RESUME = os.path.join(ROOT, "resume.txt")
DEFAULT_CACHE = os.path.join(STATE_DIR, "ai_scores.json")
DEFAULTS = {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "max_calls": 40}

PROMPT = """Ты помогаешь кандидату отбирать вакансии. Резюме и пожелания кандидата:
---
{resume}
---
Оцени вакансию для ЭТОГО кандидата (учитывай его страну проживания, уровень и стек из резюме).
Ответь ТОЛЬКО JSON-объектом:
{{"score": 0-10 (целое, насколько подходит), "level": "junior|middle|senior|lead|unknown",
"remote": true/false/null (может ли кандидат работать удалённо из своей страны или переехать по этой вакансии),
"go_stack": true/false (основной язык - Go), "reason": "одна короткая фраза по-русски",
"red_flags": ["короткие пункты по-русски: например только резиденты страны, офис, нужен опыт 5+ лет, Go не основной"]}}

Вакансия:
Источник: {source}
Компания: {company}
Название: {title}
Локация: {location}; удалёнка по данным источника: {remote}; зарплата: {salary}
Текст:
{description}"""


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def job_text(j):
    return (j.get("description") or j.get("text") or "").strip()


def cache_key(j):
    return hashlib.sha1(((j.get("url") or "") + (j.get("title") or "")).encode()).hexdigest()[:16]


def load_resume(path=None):
    path = path or DEFAULT_RESUME
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()[:4000]
    except FileNotFoundError:
        return None


def _parse(content):
    content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
    r = json.loads(content)
    return {"score": max(0, min(10, int(float(r.get("score", 0))))), "level": str(r.get("level", ""))[:20],
            "remote": r.get("remote"), "go_stack": bool(r.get("go_stack")),
            "reason": str(r.get("reason", ""))[:300],
            "red_flags": [str(x)[:120] for x in (r.get("red_flags") or [])][:6]}


def score_jobs(http, jobs, *, limit=None, settings=None, resume_path=None, cache_path=DEFAULT_CACHE,
               extra_cache=None):
    """Проставляет j["ai"] вакансиям из jobs (по порядку - важные передавайте первыми).
    Кэш - всегда (бесплатно); новых платных запросов не больше limit. Возвращает число платных запросов.
    extra_cache - старый кэш (dict) для переноса, например seen_ru.json["ai"]."""
    st = dict(DEFAULTS, **{k: v for k, v in (settings or {}).items() if v not in (None, "")})
    limit = int(st["max_calls"] if limit is None else limit)
    cache = load_json(cache_path, {})
    for k, v in (extra_cache or {}).items():
        cache.setdefault(k, v)

    todo = []
    for j in jobs:
        hit = cache.get(cache_key(j))
        if hit:
            j["ai"] = hit
        else:
            todo.append(j)
    cached = len(jobs) - len(todo)
    if not todo:
        log(f"AI: все {cached} оценок взяты из кэша")
        save_json(cache_path, cache)
        return 0

    key = (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AI_API_KEY") or "").strip()
    if not key:
        log(f"AI: нет DEEPSEEK_API_KEY - оценено только из кэша ({cached}), без оценки {len(todo)}")
        save_json(cache_path, cache)
        return 0
    resume = load_resume(resume_path)
    if not resume:
        log(f"AI: нет файла {resume_path or DEFAULT_RESUME} - создайте краткое резюме (5-15 строк). "
            f"Оценено только из кэша ({cached})")
        save_json(cache_path, cache)
        return 0

    url = st["base_url"].rstrip("/") + "/chat/completions"
    calls = 0
    for j in todo:
        if calls >= limit:
            break
        prompt = PROMPT.format(resume=resume, source=j.get("source") or "?", company=j.get("company") or "?",
                               title=j.get("title") or "?", location=j.get("location") or "?",
                               remote=j.get("remote"), salary=j.get("salary") or "не указана",
                               description=job_text(j)[:3500] or "(нет описания)")
        body = {"model": st["model"], "temperature": 0.2, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}]}
        calls += 1
        try:
            text, _ = http.request(url, headers={"Authorization": f"Bearer {key}"}, data=body, method="POST",
                                   timeout=90)
            res = _parse(json.loads(text)["choices"][0]["message"]["content"])
        except HttpError as e:
            if e.code in (401, 402, 403):
                log(f"AI: HTTP {e.code} - ключ неверен или закончился баланс. Оценка остановлена.")
                break
            log(f"AI: HTTP {e.code} для «{j.get('title')}» - пропуск")
            continue
        except (NetError, ValueError, KeyError, IndexError, TypeError) as e:
            log(f"AI: не удалось оценить «{j.get('title')}»: {e}")
            continue
        j["ai"] = res
        cache[cache_key(j)] = res
        if calls % 10 == 0:
            save_json(cache_path, cache)
    save_json(cache_path, cache)
    left = sum(1 for j in todo if not j.get("ai"))
    log(f"AI: из кэша {cached}, оплачено запросов {calls}" + (f", без оценки (лимит {limit}) {left}" if left else ""))
    return calls


def fmt_ai(a):
    """Одна строка для отчёта."""
    if not a:
        return ""
    fl = "; ".join(a.get("red_flags") or [])
    s = f"AI {a['score']}/10, {a.get('level') or '?'}, Go основной: {'да' if a.get('go_stack') else 'нет'}"
    if a.get("reason"):
        s += f" — {a['reason']}"
    if fl:
        s += f" ⚠️ {fl}"
    return s
