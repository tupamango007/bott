"""Чистые функции для преобразования текста Telegram в разметку Discord.

Модуль не зависит от запущенных ботов, поэтому его легко тестировать.
"""
import re

from discord.utils import escape_markdown

DISCORD_MESSAGE_LIMIT = 2000

# Популярные доменные зоны: «голые» домены без http:// удаляем только с ними,
# чтобы не трогать обычные слова вроде main.py, photo.jpg или Node.js.
_TLDS = (
    "com|net|org|ru|su|ua|by|kz|uz|рф|io|me|info|biz|xyz|top|site|online|"
    "shop|store|link|click|club|pro|app|dev|gg|tv|cc|co|us|uk|de|eu|fun|live|"
    "space|website|tech|ly|to|ws|win|vip|cn|in|onion"
)
URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:t\.me|telegram\.me|telegram\.dog)/\S+"
    r"|(?<![\w@.-])(?:[\w-]+\.)+(?:" + _TLDS + r")(?![\w-])(?:/\S*)?",
    re.IGNORECASE,
)
_SPACES_RE = re.compile(r"[ \t]{2,}")
_REMOVED = "\x00"  # метка на месте удалённой ссылки

# Типы сущностей, которые мы превращаем в разметку Discord.
_WRAP = {
    "bold": ("**", "**"),
    "italic": ("*", "*"),
    "underline": ("__", "__"),
    "strikethrough": ("~~", "~~"),
    "spoiler": ("||", "||"),
    "code": ("`", "`"),
}
_CODE_TYPES = {"code", "pre"}
_QUOTE_TYPES = {"blockquote", "expandable_blockquote"}


def strip_links(text):
    """Удаляет ссылки из текста, сохраняя переносы строк."""
    if not text:
        return text or ""
    cleaned = URL_RE.sub("", text)
    if cleaned == text:
        return text
    cleaned = _SPACES_RE.sub(" ", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n"))


def _etype(entity):
    return str(getattr(entity.type, "value", entity.type))


def entities_to_markdown(text, entities, remove_links=True):
    """Переводит текст с сущностями Telegram в разметку Discord.

    Смещения Telegram считаются в UTF-16, поэтому работаем с текстом в этой
    кодировке. Вложенные сущности поддерживаются, пользовательский текст
    экранируется, ссылки по желанию удаляются.
    """
    if not text:
        return ""
    data = text.encode("utf-16-le")
    size = len(data) // 2

    def chunk(start, end):
        return data[start * 2:end * 2].decode("utf-16-le")

    ents = [
        e for e in (entities or [])
        if e.length > 0 and e.offset < size
        and (_etype(e) in _WRAP or _etype(e) in _CODE_TYPES or _etype(e) in _QUOTE_TYPES
             or _etype(e) in ("url", "text_link"))
    ]
    ends = [min(e.offset + e.length, size) for e in ents]
    starts_at = {}
    for i, e in enumerate(ents):
        starts_at.setdefault(e.offset, []).append(i)
    points = sorted({0, size, *(e.offset for e in ents), *ends})

    out = []
    stack = []

    def open_tag(i):
        e = ents[i]
        t = _etype(e)
        if t in _WRAP:
            return _WRAP[t][0]
        if t == "pre":
            return f"```{getattr(e, 'language', None) or ''}\n"
        if t in _QUOTE_TYPES:
            prefix = "" if not out or "".join(out).endswith("\n") else "\n"
            return prefix + "> "
        if t == "text_link" and not remove_links:
            return "["
        return ""

    def close_tag(i):
        e = ents[i]
        t = _etype(e)
        if t in _WRAP:
            return _WRAP[t][1]
        if t == "pre":
            return "\n```"
        if t == "text_link" and not remove_links:
            return f"]({e.url})"
        return ""

    for idx, point in enumerate(points):
        ending = {i for i in stack if ends[i] == point}
        reopen = []
        while ending:
            i = stack.pop()
            out.append(close_tag(i))
            if i in ending:
                ending.discard(i)
            else:
                reopen.append(i)
        for i in reversed(reopen):
            out.append(open_tag(i))
            stack.append(i)
        for i in sorted(starts_at.get(point, []), key=lambda j: -ents[j].length):
            out.append(open_tag(i))
            stack.append(i)

        if idx + 1 >= len(points):
            break
        segment = chunk(point, points[idx + 1])
        active = {_etype(ents[i]) for i in stack}
        if "url" in active:
            if remove_links:
                out.append(_REMOVED)
                continue
            out.append(segment)
            continue
        if active & _CODE_TYPES:
            out.append(segment.replace("```", "`​``"))
            continue
        if remove_links:
            segment = strip_links(segment)
        segment = escape_markdown(segment)
        if active & _QUOTE_TYPES:
            segment = segment.replace("\n", "\n> ")
        out.append(segment)

    result = "".join(out)
    if _REMOVED in result:
        result = re.sub(r"[ \t]*\x00+[ \t]*", " ", result)
        result = re.sub(r"(?m)^ | $", "", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def plain_text(text, remove_links=True):
    """Очищает и экранирует простой текст (имена, заголовки, цитаты)."""
    if not text:
        return ""
    if remove_links:
        text = strip_links(text)
    text = _SPACES_RE.sub(" ", text).strip()
    return escape_markdown(text)


def snippet(text, limit=100):
    """Короткий однострочный фрагмент текста для цитаты."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def collect_urls(text, entities):
    """Возвращает исходный текст плюс скрытые URL из text_link-сущностей."""
    parts = [text or ""]
    for e in entities or []:
        url = getattr(e, "url", None)
        if url:
            parts.append(url)
    return "\n".join(parts)


def contains_blocked_domain(text, blocked_domains):
    """Проверяет, упоминается ли в тексте один из запрещённых доменов
    (включая поддомены), не путая example.com с notexample.com."""
    if not text:
        return False
    lowered = text.lower()
    for domain in blocked_domains:
        domain = domain.strip().lower()
        if domain and re.search(r"(?<![\w-])" + re.escape(domain) + r"(?![\w-])", lowered):
            return True
    return False


def find_ignored_word(text, words):
    """Возвращает первое найденное стоп-слово или None (без учёта регистра и ё/е)."""
    if not text:
        return None
    normalized = text.casefold().replace("ё", "е")
    for word in words:
        w = word.casefold().replace("ё", "е")
        if w and w in normalized:
            return word
    return None


def split_message(text, limit=DISCORD_MESSAGE_LIMIT):
    """Делит длинный текст на части не длиннее limit, по возможности по
    переносам строк, затем по пробелам."""
    if not text:
        return []
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n ")
    if text:
        parts.append(text)
    return [p for p in parts if p]


def format_size(num_bytes):
    return f"{num_bytes / (1024 * 1024):.0f} МБ"


def format_duration(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{seconds} с")
    return " ".join(parts)
