"""HTTP-клиент на стандартной библиотеке: ретраи с паузой, лимит запросов на хост,
общий дедлайн на запрос, certifi/SSL-подсказки для macOS, понятные ошибки.

Исключения (иерархия важна для обоих скриптов):
  NetError  - запрос не удался (после всех попыток) - результат неизвестен, не кэшировать;
  HttpError(NetError) - сервер ответил 4xx (кроме 429) - повтор не поможет; есть .code и .body;
  NotFound(HttpError) - 404/410: ресурса нет.
"""
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NetError(Exception):
    pass


class HttpError(NetError):
    def __init__(self, url, code, body=""):
        super().__init__(f"HTTP {code} {body[:150]}".strip())
        self.url, self.code, self.body = url, code, body


class NotFound(HttpError):
    pass


SSL_HINT = ("  ! Ошибка проверки SSL-сертификата. На macOS обычно помогает одно из:\n"
            "    1) pip3 install --user certifi   (скрипт сам подхватит certifi)\n"
            "    2) открыть /Applications/Python 3.x/Install Certificates.command\n"
            "    3) крайний случай - флаг --insecure (без проверки сертификатов)")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class Http:
    """
    ua            - User-Agent;
    intervals     - {хост: секунд между запросами}, default_interval - для остальных хостов;
    retries       - повторов при 429/5xx/таймаутах; backoff - база экспоненциальной паузы;
    report_errors - печатать неудачи (каждый вид ошибки на хост - один раз).
    """

    def __init__(self, ua, intervals=None, default_interval=1.0, retries=3, timeout=25, insecure=False,
                 backoff=2.0, retry_after_cap=120, report_errors=False):
        self.ua = ua
        self.intervals = dict(intervals or {})
        self.default_interval = default_interval
        self.retries, self.timeout = retries, timeout
        self.backoff, self.retry_after_cap = backoff, retry_after_cap
        self.report_errors = report_errors
        self.requests = 0
        self.errors = 0
        self._next = {}
        self._lock = threading.Lock()
        self._reported = set()
        self._ssl_hint_shown = False
        if insecure:
            self.ctx = ssl._create_unverified_context()
        else:
            try:
                import certifi  # необязательно: помогает на macOS со сборкой python.org
                self.ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                self.ctx = ssl.create_default_context()

    # ---------- служебное ----------

    def _wait(self, host):
        interval = self.intervals.get(host, self.default_interval)
        with self._lock:
            t = time.monotonic()
            slot = max(t, self._next.get(host, 0.0))
            self._next[host] = slot + interval
            self.requests += 1
        if slot > t:
            time.sleep(slot - t)

    def _ssl_hint(self):
        with self._lock:
            if self._ssl_hint_shown:
                return
            self._ssl_hint_shown = True
        _log(SSL_HINT)

    def _fail(self, host, exc):
        with self._lock:
            self.errors += 1
            key = (host, str(exc)[:50])
            new = key not in self._reported
            self._reported.add(key)
        if self.report_errors and new:
            _log(f"[сеть] {host}: {exc}")
        raise exc

    def _fetch(self, req, timeout):
        """urlopen + read с общим дедлайном: таймаут сокета не спасает, если сервер отдаёт байты по капле."""
        box = {}

        def work():
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                    raw = r.read()
                    box["ok"] = (raw.decode(r.headers.get_content_charset() or "utf-8", "replace"), r.headers)
            except BaseException as e:  # передаём в основной поток
                box["err"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout * 2 + 5)
        if t.is_alive():
            raise TimeoutError(f"ответ не получен за {timeout * 2 + 5} с")
        if "err" in box:
            raise box["err"]
        return box["ok"]

    # ---------- публичное ----------

    def request(self, url, headers=None, data=None, method=None, accept="application/json", timeout=None):
        """(текст, заголовки). data: dict/list -> JSON. Исключения: NotFound, HttpError, NetError."""
        host = urllib.parse.urlparse(url).netloc
        h = {"User-Agent": self.ua, "Accept": accept}
        h.update(headers or {})
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
        if isinstance(data, (dict, list)):
            h.setdefault("Content-Type", "application/json")
        last = "?"
        for attempt in range(self.retries + 1):
            self._wait(host)
            delay = None
            try:
                req = urllib.request.Request(url, data=body, headers=h, method=method)
                return self._fetch(req, timeout or self.timeout)
            except urllib.error.HTTPError as e:
                try:
                    txt = e.read(400).decode("utf-8", "replace").strip()
                except Exception:
                    txt = ""
                if e.code in (404, 410):
                    raise NotFound(url, e.code, txt)  # не ошибка сети: ресурса просто нет
                if e.code == 429 or e.code >= 500:
                    last = f"HTTP {e.code} {txt[:150]}".strip()
                    ra = e.headers.get("Retry-After") if e.headers else None
                    delay = min(int(ra), self.retry_after_cap) if ra and ra.isdigit() else None
                else:
                    self._fail(host, HttpError(url, e.code, txt))
            except urllib.error.URLError as e:
                last = f"{type(e.reason).__name__}: {e.reason}"
                if "CERTIFICATE_VERIFY_FAILED" in str(e.reason):
                    self._ssl_hint()
                    self._fail(host, NetError(f"SSL: {e.reason}"))
            except ssl.SSLError as e:
                last = f"SSL: {e}"
                if "CERTIFICATE_VERIFY_FAILED" in last:
                    self._ssl_hint()
                    self._fail(host, NetError(last))
            except (TimeoutError, ConnectionError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < self.retries:
                time.sleep(delay or (self.backoff * 2 ** attempt + random.random()))
        self._fail(host, NetError(f"{host}: {last} (после {self.retries + 1} попыток)"))

    def get(self, url, params=None, headers=None, accept="application/json", timeout=None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return self.request(url, headers=headers, accept=accept, timeout=timeout)[0]

    def get_json(self, url, params=None, headers=None, timeout=None):
        text = self.get(url, params, headers, timeout=timeout)
        try:
            return json.loads(text)
        except ValueError:
            raise NetError(f"{urllib.parse.urlparse(url).netloc}: ответ не JSON")

    json = get_json  # короткое имя для find_boards.py
