"""Постоянное хранилище состояния бота в JSON-файле.

Хранит чёрный список, стоп-слова, паузу, статистику и связи между
сообщениями Telegram и Discord (для правок, ответов и двусторонней пересылки).
"""
import json
import logging
import os
import tempfile

log = logging.getLogger("bridge.storage")

MAX_LINKS = 5000

DEFAULT_STATS = {
    "messages_processed": 0,
    "files_processed": 0,
    "files_skipped": 0,
    "blocked": 0,
    "rate_limited": 0,
    "edits_synced": 0,
    "discord_to_telegram": 0,
    "errors": 0,
}


class State:
    def __init__(self, path, banned=(), ignore_words=()):
        self.path = path
        self.banned = set(banned)
        self.ignore_words = list(ignore_words)
        self.paused = False
        self.stats = dict(DEFAULT_STATS)
        self.tg_to_dc = {}
        self.dc_to_tg = {}
        self.dirty = False
        self.load()

    # ---------- загрузка / сохранение ----------
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.error("Не удалось прочитать %s: %s — начинаем с пустого состояния", self.path, e)
            return
        self.banned |= {int(x) for x in data.get("banned", [])}
        if "ignore_words" in data:
            self.ignore_words = list(data["ignore_words"])
        self.paused = bool(data.get("paused", False))
        self.stats.update({k: int(v) for k, v in data.get("stats", {}).items()})
        self.tg_to_dc = dict(data.get("tg_to_dc", {}))
        self.dc_to_tg = dict(data.get("dc_to_tg", {}))

    def save(self):
        if not self.path:
            return
        data = {
            "banned": sorted(self.banned),
            "ignore_words": self.ignore_words,
            "paused": self.paused,
            "stats": self.stats,
            "tg_to_dc": self.tg_to_dc,
            "dc_to_tg": self.dc_to_tg,
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            log.error("Не удалось сохранить состояние: %s", e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def save_if_dirty(self):
        if self.dirty:
            self.save()

    # ---------- статистика ----------
    def inc(self, key, amount=1):
        self.stats[key] = self.stats.get(key, 0) + amount
        self.dirty = True

    # ---------- чёрный список ----------
    def ban(self, user_id):
        if user_id in self.banned:
            return False
        self.banned.add(user_id)
        self.save()
        return True

    def unban(self, user_id):
        if user_id not in self.banned:
            return False
        self.banned.discard(user_id)
        self.save()
        return True

    # ---------- стоп-слова ----------
    def add_word(self, word):
        word = word.strip().lower()
        if not word or word in self.ignore_words:
            return False
        self.ignore_words.append(word)
        self.save()
        return True

    def remove_word(self, word):
        word = word.strip().lower()
        if word not in self.ignore_words:
            return False
        self.ignore_words.remove(word)
        self.save()
        return True

    # ---------- пауза ----------
    def set_paused(self, value):
        self.paused = value
        self.save()

    # ---------- связи сообщений ----------
    @staticmethod
    def _tg_key(chat_id, message_id):
        return f"{chat_id}:{message_id}"

    @staticmethod
    def _trim(mapping):
        while len(mapping) > MAX_LINKS:
            mapping.pop(next(iter(mapping)))

    def link(self, chat_id, tg_message_id, channel_id, dc_message_id, **extra):
        """Связывает сообщение Telegram с сообщением Discord (в обе стороны)."""
        self.tg_to_dc[self._tg_key(chat_id, tg_message_id)] = {
            "ch": channel_id, "m": dc_message_id, **extra,
        }
        self.dc_to_tg.setdefault(str(dc_message_id), {"c": chat_id, "m": tg_message_id})
        self._trim(self.tg_to_dc)
        self._trim(self.dc_to_tg)
        self.dirty = True

    def get_dc(self, chat_id, tg_message_id):
        return self.tg_to_dc.get(self._tg_key(chat_id, tg_message_id))

    def get_tg(self, dc_message_id):
        return self.dc_to_tg.get(str(dc_message_id))

    def unlink(self, chat_id, tg_message_id):
        entry = self.tg_to_dc.pop(self._tg_key(chat_id, tg_message_id), None)
        if entry:
            self.dc_to_tg.pop(str(entry["m"]), None)
            self.dirty = True
        return entry
