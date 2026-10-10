"""Мост Telegram → Discord (и, по желанию, обратно).

Запуск: python bot.py. Все настройки — в переменных окружения, см. .env.example.
"""
import asyncio
import collections
import datetime
import io
import logging
import sys
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from telegram import BotCommand, ReplyParameters, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, InvalidToken, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from config import ConfigError, load_config
from formatting import (
    collect_urls,
    contains_blocked_domain,
    entities_to_markdown,
    find_ignored_word,
    format_duration,
    format_size,
    plain_text,
    snippet,
    split_message,
)
from keep_alive import keep_alive
from storage import State

log = logging.getLogger("bridge")

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # Bot API не отдаёт файлы больше 20 МБ
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
DISCORD_DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
DISCORD_MAX_FILES = 10
WEBHOOK_NAME = "Telegram Bridge"
NO_MENTIONS = discord.AllowedMentions.none()


@dataclass
class Media:
    label: str       # человекочитаемый тип: «фото», «видео»…
    file_id: str
    filename: str
    size: int = 0


def extract_media(msg):
    """Возвращает описание вложения сообщения Telegram или None."""
    if msg.photo:
        p = msg.photo[-1]
        return Media("фото", p.file_id, f"photo_{p.file_unique_id}.jpg", p.file_size or 0)
    if msg.animation:  # у GIF заполнены и animation, и document — проверяем раньше
        a = msg.animation
        return Media("GIF", a.file_id, a.file_name or "animation.mp4", a.file_size or 0)
    if msg.video:
        v = msg.video
        return Media("видео", v.file_id, v.file_name or "video.mp4", v.file_size or 0)
    if msg.video_note:
        v = msg.video_note
        return Media("видеосообщение", v.file_id, "video_note.mp4", v.file_size or 0)
    if msg.document:
        d = msg.document
        return Media("документ", d.file_id, d.file_name or "document.bin", d.file_size or 0)
    if msg.audio:
        a = msg.audio
        return Media("аудио", a.file_id, a.file_name or "audio.mp3", a.file_size or 0)
    if msg.voice:
        v = msg.voice
        return Media("голосовое", v.file_id, "voice.ogg", v.file_size or 0)
    if msg.sticker:
        s = msg.sticker
        if s.is_video:
            return Media("стикер", s.file_id, "sticker.webm", s.file_size or 0)
        if s.is_animated:
            # .tgs Discord не показывает — отправляем превью стикера
            if s.thumbnail:
                t = s.thumbnail
                return Media("стикер", t.file_id, "sticker.webp", t.file_size or 0)
            return None
        return Media("стикер", s.file_id, "sticker.webp", s.file_size or 0)
    return None


def describe_special(msg):
    """Текстовое описание сообщений без файла: геопозиция, контакт, опрос, кубик."""
    if msg.venue:
        v = msg.venue
        loc = v.location
        return (f"📍 **{plain_text(v.title, False)}**\n{plain_text(v.address, False)}\n"
                f"<https://maps.google.com/?q={loc.latitude},{loc.longitude}>")
    if msg.location:
        loc = msg.location
        return (f"📍 Геопозиция: {loc.latitude:.5f}, {loc.longitude:.5f}\n"
                f"<https://maps.google.com/?q={loc.latitude},{loc.longitude}>")
    if msg.contact:
        c = msg.contact
        name = " ".join(filter(None, [c.first_name, c.last_name]))
        return f"👤 Контакт: **{plain_text(name, False)}** {plain_text(c.phone_number, False)}"
    if msg.poll:
        p = msg.poll
        options = "\n".join(f"• {plain_text(o.text, False)}" for o in p.options)
        return f"📊 **Опрос:** {plain_text(p.question, False)}\n{options}"
    if msg.dice:
        return f"{msg.dice.emoji} Выпало: **{msg.dice.value}**"
    if msg.sticker and msg.sticker.emoji:
        return f"Стикер {msg.sticker.emoji}"
    return ""


def chat_link_id(chat_id):
    chat_id = str(chat_id)
    if chat_id.startswith("-100"):
        return chat_id[4:]
    return chat_id.lstrip("-")


def original_link(msg):
    """Ссылка на сообщение (если чат публичный или супергруппа) или на оригинал пересланного."""
    try:
        if msg.link:
            return msg.link
    except (AttributeError, TelegramError):
        pass
    origin = msg.forward_origin
    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)
    if chat and message_id:
        if chat.username:
            return f"https://t.me/{chat.username}/{message_id}"
        return f"https://t.me/c/{chat_link_id(chat.id)}/{message_id}"
    return None


def sender_of(msg):
    """(id, имя, username) отправителя: пользователя или чата/канала."""
    if msg.sender_chat:
        c = msg.sender_chat
        return c.id, c.title or c.full_name or "Канал", c.username
    if msg.from_user:
        u = msg.from_user
        return u.id, u.full_name, u.username
    return None, "Неизвестный", None


def forward_name(origin):
    if origin is None:
        return None
    for attr in ("sender_user", "sender_chat", "chat"):
        obj = getattr(origin, attr, None)
        if obj is not None:
            return getattr(obj, "full_name", None) or getattr(obj, "title", None)
    return getattr(origin, "sender_user_name", None) or "скрытый пользователь"


def message_body(msg, remove_links):
    text = msg.text or msg.caption or ""
    entities = msg.entities if msg.text else msg.caption_entities
    return entities_to_markdown(text, entities, remove_links=remove_links)


class RateLimiter:
    def __init__(self, count, window):
        self.count = count
        self.window = window
        self.hits = collections.defaultdict(collections.deque)
        self.warned = {}

    def allow(self, key):
        if self.count <= 0:
            return True
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.count:
            return False
        q.append(now)
        return True

    def should_warn(self, key):
        """Предупреждаем пользователя не чаще раза за окно."""
        now = time.monotonic()
        if now - self.warned.get(key, -self.window - 1) > self.window:
            self.warned[key] = now
            return True
        return False


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg.state_file, banned=cfg.banned_users, ignore_words=cfg.ignore_words)
        self.rate = RateLimiter(cfg.rate_limit_count, cfg.rate_limit_window)
        self.started_at = time.time()
        self.telegram_ready = False
        self.tg_app = None
        self.albums = {}            # media_group_id -> {"msgs": [...], "task": Task}
        self.dropped_albums = set()
        self.webhooks = {}          # channel_id -> Webhook | None
        self.tasks = set()
        self.discord = DiscordBot(self)

    # ==================== служебное ====================
    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self.tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Ошибка в фоновой задаче", exc_info=exc)
            self.state.inc("errors")
            self.spawn(self.send_log(f"Ошибка в фоновой задаче: {exc!r}", "ERROR"))

    async def send_log(self, message, level="INFO"):
        """Отправляет сообщение в лог-канал Discord, если он задан."""
        if not self.cfg.log_channel_id or not self.discord.is_ready():
            return
        channel = self.discord.get_channel(self.cfg.log_channel_id)
        if not channel:
            return
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{ts}] [{level}] {message}"
        try:
            await channel.send(split_message(text)[0], allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            log.warning("Не удалось отправить лог в Discord: %s", e)

    def health(self):
        discord_ok = self.discord.is_ready()
        return {
            "status": "ok" if discord_ok and self.telegram_ready else "degraded",
            "discord": discord_ok,
            "telegram": self.telegram_ready,
            "paused": self.state.paused,
            "uptime_seconds": int(time.time() - self.started_at),
        }

    async def autosave(self):
        while True:
            await asyncio.sleep(30)
            self.state.save_if_dirty()

    # ==================== Telegram → Discord ====================
    def chat_allowed(self, chat):
        if chat.type == ChatType.PRIVATE:
            return self.cfg.allow_private
        return chat.id in self.cfg.allowed_chats or chat.id in self.cfg.routes

    def target_channel_id(self, chat_id):
        return self.cfg.routes.get(chat_id, self.cfg.discord_channel_id)

    async def get_channel(self, channel_id):
        channel = self.discord.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.discord.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    def accepts(self, msg):
        """Общие проверки: пауза, бот, бан, белые списки."""
        if self.state.paused:
            return False
        if msg.from_user and msg.from_user.is_bot and not msg.sender_chat:
            return False
        sender_id, _, _ = sender_of(msg)
        if sender_id in self.state.banned:
            return False
        if msg.from_user and msg.from_user.id in self.state.banned:
            return False
        if self.cfg.allowed_users and (not msg.from_user or msg.from_user.id not in self.cfg.allowed_users):
            return False
        return self.chat_allowed(msg.chat)

    def blocked_reason(self, msg):
        text = msg.text or msg.caption or ""
        word = find_ignored_word(text, self.state.ignore_words)
        if word:
            return f"стоп-слово «{word}»"
        entities = msg.entities if msg.text else msg.caption_entities
        if contains_blocked_domain(collect_urls(text, entities), self.cfg.blocked_domains):
            return "запрещённый домен"
        return None

    async def check_rate(self, msg):
        sender_id, _, _ = sender_of(msg)
        if self.rate.allow(sender_id):
            return True
        self.state.inc("rate_limited")
        if msg.chat.type == ChatType.PRIVATE and self.rate.should_warn(sender_id):
            try:
                await msg.reply_text("⏳ Слишком много сообщений, подождите немного.")
            except TelegramError:
                pass
        return False

    def header(self, msg):
        _, name, username = sender_of(msg)
        line = f"{self.cfg.prefix}**{plain_text(name) or 'Без имени'}**"
        if username:
            line += f" (@{plain_text(username, False)})"
        return line

    def webhook_username(self, msg):
        _, name, username = sender_of(msg)
        name = plain_text(name).replace("\\", "") or "Telegram"
        if username:
            name += f" (@{username})"
        # Discord запрещает слова discord/clyde в имени вебхука
        for bad in ("discord", "Discord", "DISCORD", "clyde", "Clyde"):
            name = name.replace(bad, "")
        return name.strip()[:80] or "Telegram"

    def reference_for(self, msg):
        """Если исходное сообщение, на которое ответили, уже есть в Discord, — ответим на него."""
        reply = msg.reply_to_message
        if not reply or self.cfg.use_webhook:
            return None
        entry = self.state.get_dc(msg.chat.id, reply.message_id)
        if not entry:
            return None
        return discord.MessageReference(message_id=entry["m"], channel_id=entry["ch"],
                                        fail_if_not_exists=False)

    def reply_quote(self, msg):
        reply = msg.reply_to_message
        if not reply:
            return None
        _, name, _ = sender_of(reply)
        quoted = msg.quote.text if msg.quote else (reply.text or reply.caption or "")
        quoted = snippet(quoted) or "[медиа]"
        return f"> ↪ **{plain_text(name)}**: {plain_text(quoted, self.cfg.strip_links)}"

    def build_content(self, msg, *, with_header, with_quote, body_msg=None, notes=()):
        parts = []
        if with_header:
            parts.append(self.header(msg))
        fwd = forward_name(msg.forward_origin)
        if fwd:
            parts.append(f"↪ *Переслано от {plain_text(fwd)}*")
        if with_quote:
            quote = self.reply_quote(msg)
            if quote:
                parts.append(quote)
        body = message_body(body_msg or msg, self.cfg.strip_links)
        if body:
            parts.append(body)
        special = describe_special(body_msg or msg)
        if special:
            parts.append(special)
        parts.extend(notes)
        return "\n".join(parts)

    def file_limit(self, channel):
        guild = getattr(channel, "guild", None)
        limit = guild.filesize_limit if guild else DISCORD_DEFAULT_UPLOAD_LIMIT
        limit = min(limit, TELEGRAM_DOWNLOAD_LIMIT)
        if self.cfg.max_file_size:
            limit = min(limit, self.cfg.max_file_size)
        return limit

    async def download(self, bot, media, limit):
        if media.size and media.size > limit:
            return None
        try:
            file = await bot.get_file(media.file_id)
        except BadRequest as e:
            if "too big" in str(e).lower():
                return None
            raise
        if file.file_size and file.file_size > limit:
            return None
        data = bytes(await file.download_as_bytearray())
        return data if len(data) <= limit else None

    def skipped_note(self, msg, media, limit):
        link = original_link(msg)
        size = f" ({format_size(media.size)})" if media.size else ""
        if link:
            return f"⚠️ [{media.label}](<{link}>){size} — больше лимита {format_size(limit)}, ссылка на оригинал"
        return f"⚠️ {media.label}{size} — больше лимита {format_size(limit)}, ссылка недоступна"

    async def collect_files(self, msgs, channel):
        files, notes = [], []
        limit = self.file_limit(channel)
        bot = self.tg_app.bot
        for m in msgs:
            media = extract_media(m)
            if not media:
                continue
            data = await self.download(bot, media, limit)
            if data is None:
                notes.append(self.skipped_note(m, media, limit))
                self.state.inc("files_skipped")
            else:
                files.append((media.filename, data))
                self.state.inc("files_processed")
        return files, notes

    @staticmethod
    def batch_files(files, limit):
        """Делит файлы на пачки: не больше 10 штук и не больше лимита по размеру."""
        batches, current, size = [], [], 0
        for name, data in files:
            if current and (len(current) >= DISCORD_MAX_FILES or size + len(data) > limit):
                batches.append(current)
                current, size = [], 0
            current.append((name, data))
            size += len(data)
        if current:
            batches.append(current)
        return batches

    async def get_webhook(self, channel):
        if not isinstance(channel, discord.TextChannel):
            return None
        if channel.id in self.webhooks:
            return self.webhooks[channel.id]
        webhook = None
        try:
            for wh in await channel.webhooks():
                if wh.name == WEBHOOK_NAME and wh.token:
                    webhook = wh
                    break
            if webhook is None:
                webhook = await channel.create_webhook(name=WEBHOOK_NAME)
        except discord.HTTPException as e:
            log.warning("Не удалось получить вебхук в #%s (%s), шлём от имени бота", channel, e)
            await self.send_log(f"Нет права «Управлять вебхуками» в #{channel}, шлём от имени бота", "WARNING")
        self.webhooks[channel.id] = webhook
        return webhook

    async def deliver(self, channel, content, files, *, reference=None, username=None):
        """Отправляет текст и файлы в Discord, возвращает (первое сообщение, через вебхук?)."""
        chunks = split_message(content)
        batches = self.batch_files(files, self.file_limit(channel))
        plan = [[c, []] for c in chunks]
        if batches:
            if plan:
                plan[-1][1] = batches[0]
            else:
                plan.append([None, batches[0]])
            plan.extend([None, b] for b in batches[1:])

        webhook = await self.get_webhook(channel) if self.cfg.use_webhook else None
        first = None
        for i, (text, batch) in enumerate(plan):
            kwargs = {"allowed_mentions": NO_MENTIONS}
            if text:
                kwargs["content"] = text
            if batch:
                kwargs["files"] = [discord.File(io.BytesIO(data), filename=name) for name, data in batch]
            if webhook:
                sent = await webhook.send(username=username or "Telegram", wait=True, **kwargs)
            else:
                if i == 0 and reference:
                    kwargs["reference"] = reference
                    kwargs["mention_author"] = False
                sent = await channel.send(**kwargs)
            first = first or sent
        return first, webhook is not None

    async def on_tg_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not self.accepts(msg):
            return

        if msg.media_group_id:
            await self.queue_album(msg)
            return

        if not await self.check_rate(msg):
            return
        reason = self.blocked_reason(msg)
        if reason:
            log.info("Игнорируем сообщение от %s: %s", sender_of(msg)[0], reason)
            self.state.inc("blocked")
            return
        await self.forward([msg])

    async def queue_album(self, msg):
        gid = msg.media_group_id
        if gid in self.dropped_albums:
            return
        entry = self.albums.get(gid)
        if entry is None:
            if not await self.check_rate(msg):
                self.dropped_albums.add(gid)
                asyncio.get_running_loop().call_later(60, self.dropped_albums.discard, gid)
                return
            entry = self.albums[gid] = {"msgs": [], "task": None}
        entry["msgs"].append(msg)
        # Перезапускаем таймер на каждом новом фото — альбом не разорвётся на части
        if entry["task"]:
            entry["task"].cancel()
        entry["task"] = self.spawn(self.flush_album(gid))

    async def flush_album(self, gid):
        await asyncio.sleep(self.cfg.album_timeout)
        entry = self.albums.pop(gid, None)
        if not entry:
            return
        msgs = sorted(entry["msgs"], key=lambda m: m.message_id)
        for m in msgs:
            reason = self.blocked_reason(m)
            if reason:
                log.info("Игнорируем альбом от %s: %s", sender_of(m)[0], reason)
                self.state.inc("blocked")
                return
        await self.forward(msgs)

    async def forward(self, msgs):
        first = msgs[0]
        channel_id = self.target_channel_id(first.chat.id)
        channel = await self.get_channel(channel_id)
        if channel is None:
            log.error("Канал Discord %s не найден", channel_id)
            self.state.inc("errors")
            await self.send_log(f"Канал Discord {channel_id} не найден", "ERROR")
            return

        body_msg = next((m for m in msgs if m.text or m.caption), first)
        files, notes = await self.collect_files(msgs, channel)
        reference = self.reference_for(first)
        webhook_mode = self.cfg.use_webhook and isinstance(channel, discord.TextChannel)
        content = self.build_content(first, with_header=not webhook_mode,
                                     with_quote=reference is None,
                                     body_msg=body_msg, notes=notes)
        if not content and not files:
            log.info("Пустое сообщение без поддерживаемого содержимого — пропускаем")
            return

        sent, via_webhook = await self.deliver(channel, content, files, reference=reference,
                                               username=self.webhook_username(first))
        if sent:
            for m in msgs:
                self.state.link(m.chat.id, m.message_id, channel.id, sent.id,
                                wh=via_webhook, ref=reference is not None,
                                notes=notes, body=m is body_msg)
        self.state.inc("messages_processed", len(msgs))

    async def on_tg_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or self.state.paused:
            return
        entry = self.state.get_dc(msg.chat.id, msg.message_id)
        if not entry or not entry.get("body", True) or not entry.get("editable", True):
            return
        channel = await self.get_channel(entry["ch"])
        if channel is None:
            return
        webhook = await self.get_webhook(channel) if entry.get("wh") else None

        if self.blocked_reason(msg):
            # Сообщение отредактировали так, что оно теперь запрещено, — удаляем из Discord
            self.state.unlink(msg.chat.id, msg.message_id)
            self.state.inc("blocked")
            try:
                if webhook:
                    await webhook.delete_message(entry["m"])
                else:
                    await channel.get_partial_message(entry["m"]).delete()
            except discord.HTTPException as e:
                log.warning("Не удалось удалить сообщение %s: %s", entry["m"], e)
            return

        content = self.build_content(msg, with_header=not entry.get("wh"),
                                     with_quote=not entry.get("ref"),
                                     notes=entry.get("notes", []))
        chunks = split_message(content) or ["​"]
        try:
            if webhook:
                await webhook.edit_message(entry["m"], content=chunks[0], allowed_mentions=NO_MENTIONS)
            else:
                await channel.get_partial_message(entry["m"]).edit(content=chunks[0],
                                                                   allowed_mentions=NO_MENTIONS)
            self.state.inc("edits_synced")
        except discord.HTTPException as e:
            log.warning("Не удалось обновить сообщение %s: %s", entry["m"], e)

    async def on_tg_error(self, update, context: ContextTypes.DEFAULT_TYPE):
        log.error("Ошибка при обработке обновления Telegram", exc_info=context.error)
        self.state.inc("errors")
        await self.send_log(f"Ошибка Telegram: {context.error!r}", "ERROR")

    # ==================== Discord → Telegram ====================
    async def relay_to_telegram(self, message: discord.Message):
        """Ответ в Discord на пересланное сообщение уходит автору в Telegram."""
        if not self.cfg.two_way or self.state.paused or not self.telegram_ready:
            return
        if message.channel.id not in self.cfg.discord_channels:
            return
        ref = message.reference
        if not ref or not ref.message_id:
            return
        target = self.state.get_tg(ref.message_id)
        if not target:
            return

        bot = self.tg_app.bot
        reply = ReplyParameters(message_id=target["m"], allow_sending_without_reply=True)
        text = f"💬 {message.author.display_name}:\n{message.clean_content}".strip()
        try:
            sent = await bot.send_message(chat_id=target["c"], text=text[:4096], reply_parameters=reply)
            for att in message.attachments:
                if att.size > TELEGRAM_UPLOAD_LIMIT:
                    await bot.send_message(chat_id=target["c"], text=f"📎 {att.filename}: {att.url}")
                    continue
                data = await att.read()
                if (att.content_type or "").startswith("image/"):
                    await bot.send_photo(chat_id=target["c"], photo=data, reply_parameters=reply)
                else:
                    await bot.send_document(chat_id=target["c"], document=data,
                                            filename=att.filename, reply_parameters=reply)
        except TelegramError as e:
            log.warning("Не удалось отправить ответ в Telegram: %s", e)
            self.state.inc("errors")
            await message.add_reaction("❌")
            await message.reply(f"❌ Не удалось отправить в Telegram: {e}", mention_author=False)
            return
        self.state.link(target["c"], sent.message_id, message.channel.id, message.id, editable=False)
        self.state.inc("discord_to_telegram")
        await message.add_reaction("✅")

    # ==================== команды (общие для Telegram и Discord) ====================
    def cmd_pause(self, source):
        self.state.set_paused(True)
        self.spawn(self.send_log(f"Пересылка приостановлена ({source})"))
        return "⏸ Пересылка сообщений приостановлена."

    def cmd_resume(self, source):
        self.state.set_paused(False)
        self.spawn(self.send_log(f"Пересылка возобновлена ({source})"))
        return "▶ Пересылка сообщений возобновлена."

    def cmd_ban(self, user_id, source):
        if self.state.ban(user_id):
            self.spawn(self.send_log(f"Забанен пользователь Telegram {user_id} ({source})"))
            return f"✅ Пользователь {user_id} добавлен в чёрный список."
        return f"⚠️ Пользователь {user_id} уже в чёрном списке."

    def cmd_unban(self, user_id, source):
        if self.state.unban(user_id):
            self.spawn(self.send_log(f"Разбанен пользователь Telegram {user_id} ({source})"))
            return f"✅ Пользователь {user_id} удалён из чёрного списка."
        return f"⚠️ Пользователь {user_id} не найден в чёрном списке."

    def cmd_banlist(self):
        if not self.state.banned:
            return "Чёрный список пуст."
        return "🚫 Чёрный список:\n" + "\n".join(str(u) for u in sorted(self.state.banned))

    def cmd_addword(self, word, source):
        if self.state.add_word(word):
            self.spawn(self.send_log(f"Добавлено стоп-слово «{word}» ({source})"))
            return f"✅ Стоп-слово «{word.strip().lower()}» добавлено."
        return "⚠️ Такое слово уже есть или оно пустое."

    def cmd_removeword(self, word, source):
        if self.state.remove_word(word):
            self.spawn(self.send_log(f"Удалено стоп-слово «{word}» ({source})"))
            return f"✅ Стоп-слово «{word.strip().lower()}» удалено."
        return "⚠️ Такого стоп-слова нет."

    def cmd_words(self):
        if not self.state.ignore_words:
            return "Список стоп-слов пуст."
        return "🔇 Стоп-слова: " + ", ".join(self.state.ignore_words)

    def stats_lines(self):
        s = self.state.stats
        return [
            ("Обработано сообщений", s["messages_processed"]),
            ("Переслано файлов", s["files_processed"]),
            ("Пропущено файлов", s["files_skipped"]),
            ("Заблокировано фильтрами", s["blocked"]),
            ("Отсечено антифлудом", s["rate_limited"]),
            ("Синхронизировано правок", s["edits_synced"]),
            ("Ответов Discord → Telegram", s["discord_to_telegram"]),
            ("Ошибок", s["errors"]),
        ]

    def cmd_stats(self):
        return "📊 Статистика:\n" + "\n".join(f"{k}: {v}" for k, v in self.stats_lines())

    def cmd_status(self):
        latency = self.discord.latency
        ping = f" (пинг {latency * 1000:.0f} мс)" if latency == latency and latency != float("inf") else ""
        return "\n".join([
            "🤖 Статус бота",
            f"Пересылка: {'⏸ на паузе' if self.state.paused else '▶ активна'}",
            f"Discord: {'✅' if self.discord.is_ready() else '❌'}{ping}",
            f"Telegram: {'✅' if self.telegram_ready else '❌'}",
            f"Аптайм: {format_duration(time.time() - self.started_at)}",
            f"Режим отправки: {'вебхук' if self.cfg.use_webhook else 'от имени бота'}",
            f"Discord → Telegram: {'включено' if self.cfg.two_way else 'выключено'}",
            f"Маршрутов: {len(self.cfg.routes)}, в чёрном списке: {len(self.state.banned)}, "
            f"стоп-слов: {len(self.state.ignore_words)}",
        ])

    def help_text(self, prefix):
        p = prefix
        return "\n".join([
            "ℹ️ Команды администратора:",
            f"{p}pause / {p}resume — приостановить / возобновить пересылку",
            f"{p}stats — статистика, {p}status — состояние ботов",
            f"{p}ban <id> / {p}unban <id> — чёрный список Telegram, {p}banlist — показать",
            f"{p}addword <слово> / {p}removeword <слово> — стоп-слова, {p}words — показать",
        ])

    # ==================== Telegram-команды ====================
    def is_tg_admin(self, update):
        user = update.effective_user
        return bool(user and user.id in self.cfg.admin_ids)

    def tg_admin(self, func):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not self.is_tg_admin(update):
                await update.effective_message.reply_text("❌ Недостаточно прав.")
                return
            await func(update, context)
        return handler

    @staticmethod
    def _parse_id(context):
        try:
            return int(context.args[0])
        except (IndexError, ValueError):
            return None

    def build_telegram(self):
        request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
        app = Application.builder().token(self.cfg.telegram_token).request(request).build()
        only_new = filters.UpdateType.MESSAGE

        async def reply(update, text):
            await update.effective_message.reply_text(text)

        async def start(update, context):
            await reply(update, "👋 Привет! Всё, что вы сюда напишете, будет переслано в Discord.")

        async def my_id(update, context):
            await reply(update, f"Ваш ID: {update.effective_user.id}\nID чата: {update.effective_chat.id}")

        async def help_cmd(update, context):
            text = self.help_text("/") if self.is_tg_admin(update) else \
                "Просто отправьте сообщение — оно уйдёт в Discord.\n/id — узнать свой ID"
            await reply(update, text)

        async def pause(update, context):
            await reply(update, self.cmd_pause("Telegram"))

        async def resume(update, context):
            await reply(update, self.cmd_resume("Telegram"))

        async def stats(update, context):
            await reply(update, self.cmd_stats())

        async def status(update, context):
            await reply(update, self.cmd_status())

        async def ban(update, context):
            uid = self._parse_id(context)
            await reply(update, "❌ Использование: /ban <user_id>" if uid is None
                        else self.cmd_ban(uid, "Telegram"))

        async def unban(update, context):
            uid = self._parse_id(context)
            await reply(update, "❌ Использование: /unban <user_id>" if uid is None
                        else self.cmd_unban(uid, "Telegram"))

        async def banlist(update, context):
            await reply(update, self.cmd_banlist())

        async def addword(update, context):
            word = " ".join(context.args)
            await reply(update, self.cmd_addword(word, "Telegram") if word
                        else "❌ Использование: /addword <слово>")

        async def removeword(update, context):
            word = " ".join(context.args)
            await reply(update, self.cmd_removeword(word, "Telegram") if word
                        else "❌ Использование: /removeword <слово>")

        async def words(update, context):
            await reply(update, self.cmd_words())

        public = {"start": start, "help": help_cmd, "id": my_id}
        admin = {"pause": pause, "resume": resume, "stats": stats, "status": status,
                 "ban": ban, "unban": unban, "banlist": banlist,
                 "addword": addword, "removeword": removeword, "words": words}
        for name, func in public.items():
            app.add_handler(CommandHandler(name, func, filters=only_new))
        for name, func in admin.items():
            app.add_handler(CommandHandler(name, self.tg_admin(func), filters=only_new))

        app.add_handler(MessageHandler(
            (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST) & ~filters.COMMAND,
            self.on_tg_message))
        app.add_handler(MessageHandler(filters.UpdateType.EDITED, self.on_tg_edit))
        app.add_error_handler(self.on_tg_error)
        return app

    async def run_telegram(self):
        self.tg_app = self.build_telegram()
        for attempt in range(1, 6):
            try:
                await self.tg_app.initialize()
                break
            except InvalidToken:
                log.error("Неверный TELEGRAM_BOT_TOKEN")
                return
            except NetworkError as e:
                log.warning("Ошибка подключения к Telegram (попытка %d/5): %s", attempt, e)
                await asyncio.sleep(5 * attempt)
        else:
            log.error("Не удалось подключиться к Telegram после 5 попыток")
            await self.send_log("Не удалось подключиться к Telegram", "ERROR")
            return

        await self.tg_app.start()
        await self.tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        try:
            await self.tg_app.bot.set_my_commands([
                BotCommand("start", "Начать"),
                BotCommand("help", "Помощь"),
                BotCommand("id", "Узнать свой ID"),
            ])
        except TelegramError as e:
            log.warning("Не удалось установить меню команд: %s", e)

        self.telegram_ready = True
        log.info("Telegram бот запущен и слушает сообщения")
        await self.send_log("Telegram бот запущен и слушает сообщения")
        try:
            await asyncio.Event().wait()
        finally:
            self.telegram_ready = False
            await self.tg_app.updater.stop()
            await self.tg_app.stop()
            await self.tg_app.shutdown()

    # ==================== запуск ====================
    async def run_discord(self):
        try:
            await self.discord.start(self.cfg.discord_token)
        finally:
            if self.discord.is_ready():
                try:
                    await asyncio.wait_for(self.send_log("Бот останавливается", "WARNING"), 5)
                except (asyncio.TimeoutError, Exception):
                    pass
            await self.discord.close()

    async def run(self):
        if not self.cfg.admin_ids:
            log.warning("ADMIN_IDS не задан — Telegram-команды администратора недоступны никому")
        tasks = [
            asyncio.create_task(self.run_discord(), name="discord"),
            asyncio.create_task(self.run_telegram(), name="telegram"),
            asyncio.create_task(self.autosave(), name="autosave"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.state.save()
            log.info("Боты остановлены")


# ==================== Discord ====================
class DiscordBot(commands.Bot):
    def __init__(self, bridge):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=bridge.cfg.command_prefix, intents=intents,
                         help_command=None, allowed_mentions=NO_MENTIONS)
        self.bridge = bridge

    async def setup_hook(self):
        await self.add_cog(AdminCog(self.bridge))
        try:
            synced = await self.tree.sync()
            log.info("Синхронизировано %d слеш-команд", len(synced))
        except discord.HTTPException as e:
            log.error("Ошибка синхронизации слеш-команд: %s", e)

    async def on_ready(self):
        log.info("Discord бот %s подключился", self.user)
        await self.bridge.send_log(f"Discord бот {self.user} подключился")

    async def on_message(self, message):
        if message.author.bot or message.webhook_id:
            return
        ctx = await self.get_context(message)
        if ctx.valid:
            await self.invoke(ctx)
            return
        await self.bridge.relay_to_telegram(message)

    async def on_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)) \
                and not isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Недостаточно прав.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Команда работает только на сервере.", ephemeral=True)
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            cmd = ctx.command
            await ctx.send(f"❌ Использование: {ctx.clean_prefix}{cmd.qualified_name} {cmd.signature}",
                           ephemeral=True)
        else:
            log.error("Ошибка команды %s", ctx.command, exc_info=error)
            self.bridge.state.inc("errors")
            await ctx.send("❌ Произошла ошибка при выполнении команды.", ephemeral=True)


def admin_only():
    def decorator(func):
        func = commands.guild_only()(func)
        func = commands.has_permissions(administrator=True)(func)
        func = app_commands.default_permissions(administrator=True)(func)
        return func
    return decorator


class AdminCog(commands.Cog):
    """Команды работают и как !команда, и как /команда."""

    def __init__(self, bridge):
        self.bridge = bridge

    @commands.hybrid_command(name="help", description="Список команд")
    async def help(self, ctx):
        await ctx.send(self.bridge.help_text(ctx.clean_prefix if ctx.interaction is None else "/"))

    @commands.hybrid_command(name="pause", description="Приостановить пересылку")
    @admin_only()
    async def pause(self, ctx):
        await ctx.send(self.bridge.cmd_pause(f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="resume", description="Возобновить пересылку")
    @admin_only()
    async def resume(self, ctx):
        await ctx.send(self.bridge.cmd_resume(f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="stats", aliases=["show_stats", "showstats"],
                             description="Показать статистику")
    @admin_only()
    async def stats(self, ctx):
        embed = discord.Embed(title="📊 Статистика бота", color=0x00FF00)
        for name, value in self.bridge.stats_lines():
            embed.add_field(name=name, value=value)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="status", description="Состояние ботов")
    @admin_only()
    async def status(self, ctx):
        await ctx.send(self.bridge.cmd_status())

    @commands.hybrid_command(name="ban", description="Забанить пользователя Telegram по ID")
    @admin_only()
    async def ban(self, ctx, user_id: int):
        await ctx.send(self.bridge.cmd_ban(user_id, f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="unban", description="Разбанить пользователя Telegram")
    @admin_only()
    async def unban(self, ctx, user_id: int):
        await ctx.send(self.bridge.cmd_unban(user_id, f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="banlist", description="Показать чёрный список")
    @admin_only()
    async def banlist(self, ctx):
        await ctx.send(self.bridge.cmd_banlist())

    @commands.hybrid_command(name="addword", description="Добавить стоп-слово")
    @admin_only()
    async def addword(self, ctx, *, word: str):
        await ctx.send(self.bridge.cmd_addword(word, f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="removeword", description="Удалить стоп-слово")
    @admin_only()
    async def removeword(self, ctx, *, word: str):
        await ctx.send(self.bridge.cmd_removeword(word, f"Discord, {ctx.author}"))

    @commands.hybrid_command(name="words", description="Показать стоп-слова")
    @admin_only()
    async def words(self, ctx):
        await ctx.send(self.bridge.cmd_words())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("Ошибка настроек: %s", e)
        sys.exit(1)
    bridge = Bridge(cfg)
    keep_alive(bridge.health, cfg.port)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
