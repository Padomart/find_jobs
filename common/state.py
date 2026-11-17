"""Файлы состояния и кэша: атомарная запись, восстановление после повреждения."""
import json
import os
import re
import sys
import time


def load_json(path, default):
    """Читает JSON; если файла нет - default; если повреждён - сохраняет копию .bak и возвращает default."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        print(f"  ! {path} повреждён - начинаю заново (старый файл сохранён как {path}.bak)",
              file=sys.stderr, flush=True)
        os.replace(path, path + ".bak")
        return default


def save_json(path, data, indent=1, sort_keys=False):
    """Атомарная запись: сначала .tmp, потом rename - файл не останется полузаписанным."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    os.replace(tmp, path)


class Cache:
    """Кэш ответов на диске с TTL (по времени изменения файла)."""

    def __init__(self, path, disabled=False):
        self.path, self.disabled = path, disabled
        if not disabled:
            os.makedirs(path, exist_ok=True)

    def _file(self, key):
        return os.path.join(self.path, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def get(self, key, ttl_hours):
        if self.disabled:
            return None
        f = self._file(key)
        try:
            if time.time() - os.path.getmtime(f) > ttl_hours * 3600:
                return None
            with open(f, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def put(self, key, value):
        if not self.disabled:
            save_json(self._file(key), value, indent=None)
