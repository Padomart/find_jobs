"""Текстовые утилиты и фильтры, общие для en/ и ru/."""
import html
import re


def strip_html(s):
    """HTML -> текст: переносы на месте <br>/<p>/<li>, без тегов, сущности раскодированы.
    Greenhouse отдаёт HTML, экранированный дважды - раскодируем второй раз при необходимости."""
    if not s:
        return ""
    s = html.unescape(s)
    if "&lt;" in s or "&amp;" in s:
        s = html.unescape(s)
    s = re.sub(r"(?i)<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("​", "").replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


# Требуемый опыт. Англ.: "3+ years of experience", "3-5 years in backend", "at least 4 years ... experience".
YEARS_EN_RE = re.compile(
    r"(?:(?:at least|minimum(?: of)?|min\.?|over|more than)\s+)?(\d{1,2})\s*(?:\+|plus)?\s*"
    r"(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?"
    r"years?(?:\s+of)?(?:\s+\w+){0,5}?\s+(?:experience|exp\b|in\s+(?:software|backend|industry|engineering))",
    re.I)
# Рус. и короткие формы: "от 3 лет", "не менее 2 лет", "опыт 3+ года", "at least 4 years", "5+ yrs".
YEARS_RU_RE = re.compile(
    r"(?:от|не менее|более|минимум|from|at least)\s*(\d{1,2})(?:[,.]5)?\s*(?:\+\s*)?(?:-?х\s*)?(?:лет|год|years?|yrs)"
    r"|(\d{1,2})\+\s*(?:лет|год|years?|yrs)", re.I)


def years_required(text):
    """Наибольшее из найденных минимальных требований к опыту (лет) или None, если не сказано."""
    best = None
    for rx in (YEARS_EN_RE, YEARS_RU_RE):
        for m in rx.finditer(text or ""):
            n = int(m.group(1) or m.group(2))
            if 1 <= n <= 15:
                best = n if best is None else max(best, n)
    return best


DEFAULT_MAX_YEARS = 5  # у кандидата ~2 года; 3-5 допустимо для Middle


# ---------- Починка текста: кракозябры, пробелы ----------

# UTF-8, ошибочно прочитанный как latin-1/cp1252: "â\x80\x99" вместо "’", "Ã©" вместо "é", "ð\x9f\x9a\x80" - эмодзи
_CP1252_EXTRA = "ŒœŠšŸŽžƒˆ˜–—‘’‚“" \
                "”„†‡•…‰‹›€™"
MOJIBAKE_RE = re.compile(f"[Â-ô][\u0080-¿{_CP1252_EXTRA}]{{1,3}}")


def _unmojibake(m):
    s = m.group(0)
    for enc in ("latin-1", "cp1252"):
        try:
            return s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return s


def fix_mojibake(text):
    """Чинит типичные кракозябры (RemoteOK: "Weâ\\x80\\x99re" -> "We’re"); нормальный текст не трогает."""
    if not text or not MOJIBAKE_RE.search(text):
        return text or ""
    return MOJIBAKE_RE.sub(_unmojibake, text)


def clean_text(text):
    """Кракозябры, невидимые символы, неразрывные пробелы; пробелы по краям строк; не больше одной пустой строки."""
    t = fix_mojibake(text or "")
    t = re.sub(r"[﻿​‌‍⁠­]", "", t)
    t = re.sub(r"[   \t]", " ", t)
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", t)
    lines = [re.sub(r" {2,}", " ", ln).strip() for ln in t.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    t = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", t).strip()
