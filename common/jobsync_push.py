#!/usr/bin/env python3
"""
Единый клиент JobSync MCP: add_jobs_batch батчами до 10, find_job, учёт лимита.

Лимит JobSync - 60 вызовов инструментов в час на ВСЕ токены, и add_jobs_batch тратит по
одному вызову на каждую вакансию. Поэтому оба инструмента (boards/ и ru/) ведут один общий
учёт state/jobsync_ledger.json: что отправлено и сколько потрачено за последний час.

Настройка - только переменные окружения, в коде и файлах ничего не хранится:
  JOBSYNC_URL    адрес JobSync (с /api/mcp или без)
  JOBSYNC_TOKEN  токен из Settings -> MCP Access

Запуск как программы - отправить вакансии без ATS, найденные boards/find_boards.py:
  python3 common/jobsync_push.py --dry-run
  python3 common/jobsync_push.py --limit 20 --go-only
  python3 common/jobsync_push.py --input ru/out/jobs_ru.json --limit 5
  python3 common/jobsync_push.py --resend            # обновить уже отправленные (описание и т.п.)

Описание уходит в поле jobDescription как Markdown: JobSync сам превращает его в HTML (markdown-it,
сырой HTML экранируется). Сверху - служебные строки: источник, ссылка на оригинал, регион, стек, опыт.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

if __package__ in (None, ""):  # запуск файлом: python3 common/jobsync_push.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import JOBSYNC_LEDGER, ROOT  # noqa: E402
from common.httpclient import Http, HttpError, NetError  # noqa: E402
from common.filters import clean_text  # noqa: E402
from common.state import load_json, save_json  # noqa: E402

WINDOW = 3600
BATCH_MAX = 10
HARD_LIMIT = 60
UA = "job-search-jobsync/1.0 (personal job search script; python-urllib)"
MIN_DESC = 300  # --max-desc меньше этого не режет (иначе описание обнулится); у JobSync минимум 10 символов


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Ledger:
    """Учёт расхода лимита (метки времени единиц за последний час) и уже отправленных ссылок."""

    def __init__(self, path=JOBSYNC_LEDGER):
        self.path = path
        d = load_json(path, {})
        self.units = [t for t in d.get("units", []) if time.time() - t < WINDOW]
        self.pushed = d.get("pushed", {})

    def used(self):
        self.units = [t for t in self.units if time.time() - t < WINDOW]
        return len(self.units)

    def spend(self, n):
        self.units += [time.time()] * n
        self.save()

    def wait_seconds(self, need, budget):
        """Сколько секунд ждать, пока освободится need единиц."""
        self.used()
        over = len(self.units) + need - budget
        if over <= 0:
            return 0
        return max(0, WINDOW - (time.time() - sorted(self.units)[over - 1])) + 5

    def minutes_until(self, need, budget):
        w = self.wait_seconds(need, budget)
        return int(w // 60) + 1 if w else 0

    def save(self):
        save_json(self.path, {"units": self.units, "pushed": self.pushed})


class McpClient:
    """JSON-RPC поверх Streamable HTTP. JobSync работает stateless и отвечает обычным JSON;
    SSE-ответ тоже поддерживается."""

    def __init__(self, http, base_url, token, client_name="job-search"):
        base = base_url.rstrip("/")
        self.url = base if base.endswith("/api/mcp") else base + "/api/mcp"
        self.http, self.token, self.client_name = http, token, client_name
        self.session, self._id = None, 0

    def _post(self, payload):
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        try:
            text, headers = self.http.request(self.url, headers=h, data=payload, method="POST", timeout=120)
        except HttpError as e:
            hint = {401: "токен неверный или истёк", 404: "MCP выключен (нужен MCP_ENABLED=true) или неверный JOBSYNC_URL",
                    429: "превышен лимит JobSync (60/час)"}.get(e.code, e.body[:200])
            raise SystemExit(f"JobSync: HTTP {e.code} - {hint}")
        except NetError as e:
            raise SystemExit(f"JobSync недоступен: {e}")
        self.session = headers.get("Mcp-Session-Id") or self.session
        if not text.strip():
            return None
        if "text/event-stream" in (headers.get("Content-Type") or ""):
            msgs = [json.loads(x[5:]) for x in text.splitlines() if x.startswith("data:")]
            res = next((m for m in msgs if "result" in m or "error" in m), None)
        else:
            res = json.loads(text)
        if isinstance(res, dict) and res.get("error"):
            raise SystemExit(f"JobSync MCP ошибка: {res['error']}")
        return res

    def initialize(self):
        """initialize не расходует лимит инструментов."""
        self._id += 1
        self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": self.client_name, "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, args):
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}}) or {}
        res = r.get("result") or {}
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))


# ---------- Описание вакансии для JobSync ----------
# JobSync (createJobFromNames.ts) сохраняет jobDescription как md.render(текст) - markdown-it с
# html:false и breaks:true. Значит, отправлять надо Markdown, а не HTML: HTML-теги markdown-it
# экранирует, и в карточке они показались бы как текст "<p>...".

BULLET_RE = re.compile(r"^\s*(?:[-–—•·▪●◦*✓✔➤►]|\d{1,2}[.)])\s+(.*)$")
MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>|])")


def _md_escape(s):
    """Экранирует символы разметки Markdown, чтобы обычный текст не превратился в курсив/ссылки/заголовки."""
    s = MD_SPECIAL_RE.sub(r"\\\1", s)
    s = re.sub(r"^(#{1,6}|=+|-{3,}|\+)(\s|$)", r"\\\1\2", s)  # строка-заголовок / линия
    return re.sub(r"^(\d{1,3})([.)])(\s)", r"\1\\\2\3", s)  # "2024. год" - не нумерованный список


def text_to_markdown(text):
    """Обычный текст -> Markdown: строки становятся абзацами, строки с маркерами "-", "•", "1." - списком."""
    out, prev_list = [], False
    for line in clean_text(text).split("\n"):
        if not line:
            if out and out[-1] != "":
                out.append("")
            prev_list = False
            continue
        m = BULLET_RE.match(line)
        if m and m.group(1).strip():
            if not prev_list and out and out[-1] != "":
                out.append("")
            out.append("- " + _md_escape(m.group(1).strip()))
            prev_list = True
        else:
            if out and out[-1] != "":
                out.append("")  # каждая строка - отдельный абзац (в исходниках переносы и есть абзацы)
            out.append(_md_escape(line))
            prev_list = False
    return "\n".join(out).strip()


def build_description(text, header=(), url="", max_desc=15000):
    """Markdown для jobDescription: служебные строки сверху, затем текст вакансии, обрезанный по max_desc
    по границе абзаца. Никогда не возвращает пустую строку: без текста - только шапка или 'N/A'."""
    max_desc = max(int(max_desc or 0), MIN_DESC)
    head = [h for h in header if h]
    if url:
        head.append(f"Оригинал: <{url}>")
    head_md = "\n".join(f"{line}  " for line in head).rstrip()  # два пробела - перенос строки в Markdown
    body = text_to_markdown(text or "")
    tail = "\n\n_…текст обрезан, полностью — по ссылке на оригинал._"
    room = max_desc - len(head_md) - 10
    if len(body) > room:
        cut = body[:max(0, room - len(tail))]
        p = cut.rfind("\n\n")
        body = (cut[:p] if p > len(cut) * 0.6 else cut.rsplit(" ", 1)[0]).rstrip() + tail
    parts = [x for x in (head_md, "---" if head_md and body else "", body) if x]
    desc = "\n\n".join(parts).strip()
    return desc if len(desc) >= 10 else "N/A"


def _batch_status(text, items):
    """{индекс: (строка ответа, статус в нижнем регистре)} для ответа add_jobs_batch."""
    out = {}
    for b in re.split(r"(?m)^(?=\[\d+/\d+\] )", text):
        m = re.match(r"\[(\d+)/\d+\] ", b)
        if not m:
            continue
        n = int(m.group(1)) - 1
        line = b.strip()
        if 0 <= n < len(items):
            prefix = f"[{n + 1}/{len(items)}] {items[n]['company']} — {items[n]['jobTitle']}: "
            st = line[len(prefix):] if line.startswith(prefix) else line.split(": ", 1)[-1]
            out[n] = (line, st.lower())
    return out


def push_jobs(jobs, to_item, *, budget=50, batch=BATCH_MAX, check_existing=False, dry_run=False,
              wait=False, ledger=None, http=None, client_name="job-search", describe=None, resend=False):
    """Отправляет вакансии в JobSync. jobs - список dict с ключом "url"; to_item(job) -> аргументы add_job.
    Возвращает число добавленных/обновлённых. URL/токен - из JOBSYNC_URL/JOBSYNC_TOKEN.
    resend=True - отправить и уже отправленные (учёт не фильтрует): с upsert:true JobSync находит запись
    по URL и обновляет её поля, включая описание, - дублей не создаётся."""
    ledger = ledger or Ledger()
    budget = max(1, min(int(budget), HARD_LIMIT))
    batch = max(1, min(int(batch), BATCH_MAX))
    describe = describe or (lambda j: f"{j.get('company')} - {j.get('title')}")
    todo = [j for j in jobs if j.get("url") and (resend or j["url"] not in ledger.pushed)]
    if resend and check_existing:
        log("JobSync: --resend - проверка find_job не нужна (upsert обновит существующую запись), пропускаю её")
        check_existing = False
    cost = len(todo) * (2 if check_existing else 1)
    log(f"JobSync: к отправке {len(todo)} (ранее отправлено: {len(ledger.pushed)}); нужно до {cost} вызовов, "
        f"израсходовано за час (локальный учёт): {ledger.used()}/{budget}")
    if not todo:
        return 0
    if dry_run:
        for n, j in enumerate(todo):
            log(f"  [dry-run] {describe(j)}  {j['url']}")
            if n < 3:  # что именно уйдёт в add_jobs_batch (первые 3)
                log(json.dumps(to_item(j), ensure_ascii=False, indent=2))
        return 0
    base, token = os.environ.get("JOBSYNC_URL", "").strip(), os.environ.get("JOBSYNC_TOKEN", "").strip()
    if not base or not token:
        log("JobSync: задайте переменные окружения JOBSYNC_URL и JOBSYNC_TOKEN (Settings -> MCP Access). "
            "Отправка пропущена.")
        return 0
    client = McpClient(http or Http(UA, retries=2, timeout=60), base, token, client_name)
    client.initialize()

    def room(need):
        w = ledger.wait_seconds(need, budget)
        if not w:
            return True
        if not wait:
            log(f"JobSync: лимит на час почти исчерпан ({ledger.used()}/{budget}); продолжите через "
                f"{int(w // 60) + 1} мин. (или --wait) - отправленное не повторится.")
            return False
        log(f"JobSync: жду {int(w)} с, пока освободится лимит...")
        time.sleep(w)
        return True

    if check_existing:
        fresh = []
        for j in todo:
            if not room(2):  # место хотя бы на проверку и добавление
                break
            text, _ = client.call("find_job", {"jobUrl": j["url"]})
            ledger.spend(1)
            low = text.lower()
            if "rate limit" in low:
                log(f"JobSync: {text[:200]}")
                break
            if low.startswith("no saved job") or "not found" in low or "no job" in low:
                fresh.append(j)
            else:
                ledger.pushed[j["url"]] = now_iso()
                log(f"  уже есть в JobSync: {describe(j)}")
        ledger.save()
        todo = fresh

    added, i = 0, 0
    while i < len(todo):
        free = budget - ledger.used()
        if free <= 0:
            if not room(1):
                break
            continue
        chunk = todo[i:i + min(batch, free)]  # не больше, чем осталось в лимите
        i += len(chunk)
        items = [to_item(j) for j in chunk]
        text, is_err = client.call("add_jobs_batch", {"jobs": items})
        ledger.spend(len(chunk))
        st = _batch_status(text, items)
        for n, j in enumerate(chunk):
            line, status = st.get(n, ("", ""))
            if is_err or not line or status.startswith(("error", "validation error", "rate limit", "insufficient")):
                log(f"  не добавлено: {describe(j)}: {line[:200] or text[:200] or 'нет ответа'}")
                continue
            ledger.pushed[j["url"]] = now_iso()
            added += 1
            log(f"  ок: {line.splitlines()[0][:400]}")
        ledger.save()
        if is_err or "rate limit exceeded" in text.lower():
            log("JobSync сообщил об ошибке/лимите - остановка. Запустите позже, отправленное не повторится.")
            break
    log(f"JobSync: добавлено/обновлено {added}; израсходовано за час {ledger.used()}/{budget}")
    return added


# ---------- CLI: вакансии без ATS из boards/out/jobs_noats.json ----------

def job_header(j):
    """Служебные строки описания. Понимает оба формата: boards/out/jobs_noats.json (text, regions, go, py,
    years, posted) и ru/out/jobs_ru.json (description, salary, published_at, rf_limits, go_role, ai)."""
    if j.get("remote"):
        regs = ", ".join(j.get("regions") or [])
        where = "Remote" + (f" ({regs})" if regs else "")
        if j.get("location") and not regs:
            where += f", {j['location']}"
    else:
        where = j.get("location") or ""
    if j.get("restrict"):
        where += f"; ограничение: {j['restrict']}"
    if j.get("visa"):
        where += "; спонсорство визы"
    elif j.get("relocation"):
        where += "; релокация"
    text = f"{j.get('title', '')} {j.get('text') or j.get('description') or ''}"
    go = j.get("go") if "go" in j else (j.get("go_role") or (1 if re.search(r"golang|\bgo\b", text, re.I) else 0))
    py = j.get("py") if "py" in j else bool(re.search(r"\bpython\b", text, re.I))
    stack = [x for x, ok in (("Go", go), ("Python", py)) if ok]
    if j.get("go_role") == 1:
        stack.append("Go не основной язык")
    years = j.get("years")
    level = ", ".join(x for x in ((j.get("level") or ""), f"опыт от {years} лет" if years else "") if x)
    posted = (j.get("posted") or j.get("published_at") or "")[:10]
    ai = j.get("ai") or {}
    return [
        f"**Источник:** {j.get('source') or '?'}" + (f" · опубликовано {posted}" if posted else ""),
        f"**Где:** {where}" if where else "",
        f"**Зарплата:** {j['salary']}" if j.get("salary") else "",
        " · ".join(x for x in (f"**Стек:** {', '.join(stack)}" if stack else "",
                               f"**Уровень:** {level}" if level else "") if x),
        f"⚠️ **Ограничения:** {', '.join(j['rf_limits'])}" if j.get("rf_limits") else "",
        "⚠️ Текст в источнике неполный — проверьте оригинал." if j.get("text_incomplete") else "",
        f"**AI {ai.get('score')}/10:** {ai.get('reason', '')}" if ai else "",
    ]


def noats_item(j, max_desc=15000):
    if "description" in j and "text" not in j:  # ru/out/jobs_ru.json - те же поля, что шлёт ru/find_ru_jobs.py
        tags = (["Go"] if re.search(r"golang|\bgo\b", f"{j['title']} {j['description']}", re.I) else [])
        item = {"company": j["company"][:200], "jobTitle": j["title"][:300],
                "jobDescription": build_description(j["description"], job_header(j), j["url"], max_desc),
                "source": f"find_ru_jobs:{j.get('source')}", "jobUrl": j["url"],
                "tags": tags + ["RU", j.get("source") or "ru"], "upsert": True}
        if j.get("location"):
            item["location"] = j["location"][:200]
        if j.get("remote"):
            item["workplaceType"] = "Remote"
        return item
    tags = []
    if j.get("go"):
        tags.append("Go")
    if j.get("py"):
        tags.append("Python")
    tags += [t for t in (j.get("tags") or []) if isinstance(t, str) and t.lower() not in ("go", "golang", "python")]
    item = {
        "company": j["company"][:200],
        "jobTitle": j["title"][:300],
        "jobDescription": build_description(j.get("text") or j.get("description") or "", job_header(j),
                                            j.get("url") or "", max_desc),
        "source": j.get("source") or "find_boards",
        "jobUrl": j["url"],
        "tags": tags[:10],
        "upsert": True,
    }
    if j.get("location"):
        item["location"] = j["location"][:200]
    if j.get("remote"):
        item["workplaceType"] = "Remote"
    return item


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                add_help=False)
    p._optionals.title = "параметры"
    p.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")
    p.add_argument("--input", default=os.path.join(ROOT, "boards", "out", "jobs_noats.json"),
                   help="файл вакансий (по умолчанию boards/out/jobs_noats.json)")
    p.add_argument("--ledger", default=JOBSYNC_LEDGER, help="общий учёт лимита (state/jobsync_ledger.json)")
    p.add_argument("--limit", type=int, default=30, help="сколько вакансий отправить за запуск (30)")
    p.add_argument("--budget", type=int, default=50,
                   help="не тратить больше N вызовов в час (лимит JobSync 60; по умолчанию 50 - запас для вас)")
    p.add_argument("--batch", type=int, default=BATCH_MAX, help="размер батча, максимум 10")
    p.add_argument("--go-only", action="store_true", help="только вакансии с Go")
    p.add_argument("--remote-only", action="store_true", help="только remote")
    p.add_argument("--check-existing", action="store_true",
                   help="перед добавлением проверять find_job (1 вызов лимита на вакансию)")
    p.add_argument("--wait", action="store_true", help="если лимит исчерпан - ждать, а не завершаться")
    p.add_argument("--max-desc", type=int, default=15000,
                   help=f"обрезать описание до N символов (по умолчанию 15000, не меньше {MIN_DESC})")
    p.add_argument("--resend", action="store_true",
                   help="повторно отправить УЖЕ отправленные вакансии (по учёту) - обновить их в JobSync "
                        "через upsert, без дублей; например, чтобы дописать описание")
    p.add_argument("--min-ai", type=float, metavar="N",
                   help="только вакансии с AI-оценкой не ниже N (запустите поиск с --ai); без оценки - пропускаются")
    p.add_argument("--url", action="append", default=[], metavar="URL",
                   help="только вакансии с этой ссылкой (можно несколько раз)")
    p.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только показать")
    a = p.parse_args(argv)
    jobs = load_json(a.input, None)
    if jobs is None:
        raise SystemExit(f"{a.input} не найден - сначала запустите boards/find_boards.py с --sources "
                         f"(или ru/find_ru_jobs.py для ru/out/jobs_ru.json)")
    ledger = Ledger(a.ledger)
    todo = [j for j in jobs if j.get("url") and j.get("company") and j.get("title")
            and ((j["url"] in ledger.pushed) if a.resend else (j["url"] not in ledger.pushed))
            and (not a.url or j["url"] in a.url)
            and (a.min_ai is None or (j.get("ai") or {}).get("score", -1) >= a.min_ai)
            and (not a.go_only or j.get("go") or j.get("go_role")) and (not a.remote_only or j.get("remote"))][: a.limit]
    if a.resend:
        log(f"--resend: уже отправленных вакансий в {os.path.basename(a.input)}: {len(todo)}")
    push_jobs(todo, lambda j: noats_item(j, a.max_desc), budget=a.budget, batch=a.batch,
              check_existing=a.check_existing, dry_run=a.dry_run, wait=a.wait, ledger=ledger, resend=a.resend,
              client_name="find_boards",
              describe=lambda j: f"{j['company']} | {j['title']} | {j.get('location') or '?'} | {j.get('source')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
