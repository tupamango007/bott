"""Загрузка и проверка настроек из переменных окружения (и файла .env)."""
import os
import re
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv необязателен
    load_dotenv = None


class ConfigError(Exception):
    pass


def _raw(name, default=""):
    return os.environ.get(name, default).strip()


def _split(value):
    return [p.strip() for p in re.split(r"[,;\n]", value) if p.strip()]


def _int(name, default=0):
    value = _raw(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} должен быть числом, получено: {value!r}")


def _float(name, default):
    value = _raw(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} должен быть числом, получено: {value!r}")


def _bool(name, default):
    value = _raw(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "да")


def _int_list(name):
    result = []
    for item in _split(_raw(name).replace(" ", ",")):
        try:
            result.append(int(item))
        except ValueError:
            raise ConfigError(f"{name}: {item!r} не является числом")
    return result


def _str_list(name, default):
    value = os.environ.get(name)
    if value is None:
        return list(default)
    return _split(value)


def _routes(name):
    """ROUTES="-1001234:111222333, -1005678:444555666" → {tg_chat: discord_channel}."""
    routes = {}
    for item in _split(_raw(name)):
        try:
            tg, dc = item.split(":")
            routes[int(tg)] = int(dc)
        except ValueError:
            raise ConfigError(f"{name}: неверный маршрут {item!r}, нужен формат tg_chat_id:discord_channel_id")
    return routes


@dataclass
class Config:
    telegram_token: str
    discord_token: str
    discord_channel_id: int
    log_channel_id: int = 0
    routes: dict = field(default_factory=dict)
    prefix: str = "📢 "
    command_prefix: str = "!"
    ignore_words: list = field(default_factory=list)
    blocked_domains: list = field(default_factory=list)
    allowed_users: list = field(default_factory=list)
    allowed_chats: list = field(default_factory=list)
    allow_private: bool = True
    admin_ids: list = field(default_factory=list)
    banned_users: list = field(default_factory=list)
    max_file_size: int = 0
    album_timeout: float = 1.0
    rate_limit_count: int = 10
    rate_limit_window: float = 60.0
    use_webhook: bool = False
    two_way: bool = False
    strip_links: bool = True
    state_file: str = "bot_state.json"
    port: int = 8080

    @property
    def discord_channels(self):
        channels = set(self.routes.values())
        if self.discord_channel_id:
            channels.add(self.discord_channel_id)
        return channels


def load_config():
    if load_dotenv:
        load_dotenv()

    telegram_token = _raw("TELEGRAM_BOT_TOKEN")
    discord_token = _raw("DISCORD_BOT_TOKEN")
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", telegram_token),
                              ("DISCORD_BOT_TOKEN", discord_token)) if not v]
    if missing:
        raise ConfigError("Не заданы переменные окружения: " + ", ".join(missing))

    routes = _routes("ROUTES")
    channel_id = _int("DISCORD_CHANNEL_ID")
    if not channel_id and not routes:
        raise ConfigError("Нужно задать DISCORD_CHANNEL_ID или ROUTES")

    allowed_chats = _int_list("ALLOWED_CHATS")
    # По умолчанию: если группы не указаны — пересылаем только личные сообщения,
    # если указаны — только из них (как и раньше). ALLOW_PRIVATE меняет поведение.
    allow_private = _bool("ALLOW_PRIVATE", not (allowed_chats or routes))

    return Config(
        telegram_token=telegram_token,
        discord_token=discord_token,
        discord_channel_id=channel_id,
        log_channel_id=_int("LOG_CHANNEL_ID"),
        routes=routes,
        prefix=os.environ.get("MESSAGE_PREFIX", "📢 "),
        command_prefix=_raw("COMMAND_PREFIX", "!") or "!",
        ignore_words=_str_list("IGNORE_WORDS", ["спам", "реклама"]),
        blocked_domains=_str_list("BLOCKED_DOMAINS", []),
        allowed_users=_int_list("ALLOWED_USERS"),
        allowed_chats=allowed_chats,
        allow_private=allow_private,
        admin_ids=_int_list("ADMIN_IDS"),
        banned_users=_int_list("BANNED_USERS"),
        max_file_size=_int("MAX_FILE_SIZE_MB", 0) * 1024 * 1024,
        album_timeout=_float("ALBUM_TIMEOUT", 1.0),
        rate_limit_count=_int("RATE_LIMIT_COUNT", 10),
        rate_limit_window=_float("RATE_LIMIT_WINDOW", 60.0),
        use_webhook=_bool("USE_WEBHOOK", False),
        two_way=_bool("TWO_WAY", False),
        strip_links=_bool("STRIP_LINKS", True),
        state_file=_raw("STATE_FILE", "bot_state.json") or "bot_state.json",
        port=_int("PORT", 8080),
    )
