import asyncio
import io
import os
import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, CommandHandler, ContextTypes as TGContext
from telegram.error import BadRequest, TimedOut
from telegram.request import HTTPXRequest
import discord
from discord.ext import commands
from keep_alive import keep_alive

# ==================== НАСТРОЙКИ (переменные окружения) ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", 0))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))

PREFIX = "📢 "
IGNORE_WORDS = ["спам", "реклама"]
IGNORE_USERS = []

ALLOWED_USERS = []          # пусто – все разрешены
ALLOWED_CHATS = []          # пусто – только личные сообщения

MAX_FILE_SIZE = 10 * 1024 * 1024        # 10МБ
ALBUM_TIMEOUT = 0.5

ADMIN_IDS = []              # ID пользователей Telegram для команд (пусто – всем)
bot_paused = False
stats = {
    'messages_processed': 0,
    'files_processed': 0,
    'files_skipped': 0,
    'errors': 0
}
# =============================================================================

pending_albums = {}
album_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
discord_client = commands.Bot(command_prefix="!", intents=intents)

# ---------- Функция отправки логов в Discord ----------
async def send_log(message, level="INFO"):
    """Отправляет сообщение в лог-канал Discord, если он задан."""
    if not LOG_CHANNEL_ID:
        return
    channel = discord_client.get_channel(LOG_CHANNEL_ID)
    if channel:
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{timestamp}] [{level}] {message}"
        if len(formatted) > 2000:
            formatted = formatted[:1997] + "..."
        try:
            await channel.send(formatted)
        except Exception as e:
            print(f"Не удалось отправить лог в Discord: {e}")

# ---------- Вспомогательные функции ----------
def format_text_with_entities(text, entities):
    if not entities:
        return text
    sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)
    result = list(text)
    for entity in sorted_entities:
        start = entity.offset
        end = entity.offset + entity.length
        substring = text[start:end]
        if entity.type == 'bold':
            result[start:end] = f"**{substring}**"
        elif entity.type == 'italic':
            result[start:end] = f"*{substring}*"
        elif entity.type == 'underline':
            result[start:end] = f"*{substring}*"
        elif entity.type == 'strikethrough':
            result[start:end] = f"~~{substring}~~"
        elif entity.type == 'code':
            result[start:end] = f"`{substring}`"
        elif entity.type == 'pre':
            result[start:end] = f"```{substring}```"
        elif entity.type == 'text_link':
            result[start:end] = f"[{substring}]({entity.url})"
    return ''.join(result)

def get_forward_link(msg):
    try:
        if not hasattr(msg, 'forward_origin') or not msg.forward_origin:
            return None
        origin = msg.forward_origin
        if hasattr(origin, 'chat') and origin.chat:
            chat = origin.chat
            message_id = getattr(origin, 'message_id', None)
            if chat and message_id:
                if chat.username:
                    return f"https://t.me/{chat.username}/{message_id}"
                else:
                    chat_id = str(chat.id)
                    if chat_id.startswith('-100'):
                        chat_id = chat_id[4:]
                    elif chat_id.startswith('-'):
                        chat_id = chat_id[1:]
                    return f"https://t.me/c/{chat_id}/{message_id}"
        return None
    except Exception as e:
        print(f"⚠️ Ошибка при получении ссылки на пересланное: {e}")
        return None

def get_sender_info(user):
    name = user.full_name
    username = f"(@{user.username})" if user.username else ""
    return f"**{name}** {username}".strip()

def get_message_link(msg):
    try:
        if hasattr(msg, 'link') and msg.link:
            return msg.link
    except:
        pass
    chat = msg.chat
    if chat.type in ['group', 'supergroup', 'channel']:
        chat_id = str(chat.id)
        if chat_id.startswith('-100'):
            chat_id = chat_id[4:]
        elif chat_id.startswith('-'):
            chat_id = chat_id[1:]
        return f"https://t.me/c/{chat_id}/{msg.message_id}"
    return None

def split_long_message(text, max_len=2000):
    if len(text) <= max_len:
        return text
    return text[:max_len-3] + "..."

async def download_file(file_id, bot):
    try:
        file = await bot.get_file(file_id)
    except BadRequest as e:
        if "File is too big" in str(e):
            print(f"⚠️ Файл {file_id} слишком большой (ошибка API), пропускаем.")
            await send_log(f"Файл {file_id} слишком большой, пропущен", "WARNING")
            return None
        else:
            raise
    if hasattr(file, 'file_size') and file.file_size > MAX_FILE_SIZE:
        print(f"⚠️ Файл {file_id} слишком большой ({file.file_size} байт), пропускаем.")
        await send_log(f"Файл {file_id} размером {file.file_size} байт превышает лимит", "WARNING")
        return None
    file_bytes = await file.download_as_bytearray()
    if len(file_bytes) > MAX_FILE_SIZE:
        print(f"⚠️ Скачанный файл {file_id} превышает лимит, пропускаем.")
        await send_log(f"Скачанный файл {file_id} превышает лимит", "WARNING")
        return None
    return io.BytesIO(file_bytes)

# ---------- Отправка альбома ----------
async def send_album(media_group_id, channel):
    async with album_lock:
        if media_group_id not in pending_albums:
            return
        messages = pending_albums.pop(media_group_id, [])
    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    sender = messages[0].from_user
    sender_info = get_sender_info(sender)
    bot = messages[0].get_bot()

    discord_files = []
    skipped_links = []
    caption = None

    for msg in messages:
        if not caption:
            caption = msg.text or msg.caption or ""
            if msg.entities:
                caption = format_text_with_entities(caption, msg.entities)
            if msg.caption_entities:
                caption = format_text_with_entities(caption, msg.caption_entities)

        if msg.photo:
            photo = msg.photo[-1]
            file_data = await download_file(photo.file_id, bot)
            if file_data:
                discord_files.append(("photo", file_data, "photo.jpg"))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "фото"))
                stats['files_skipped'] += 1
        elif msg.video:
            file_data = await download_file(msg.video.file_id, bot)
            if file_data:
                filename = msg.video.file_name or "video.mp4"
                discord_files.append(("video", file_data, filename))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "видео"))
                stats['files_skipped'] += 1
        elif msg.document:
            file_data = await download_file(msg.document.file_id, bot)
            if file_data:
                filename = msg.document.file_name or "document.bin"
                discord_files.append(("document", file_data, filename))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "документ"))
                stats['files_skipped'] += 1
        elif msg.audio:
            file_data = await download_file(msg.audio.file_id, bot)
            if file_data:
                filename = msg.audio.file_name or "audio.mp3"
                discord_files.append(("audio", file_data, filename))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "аудио"))
                stats['files_skipped'] += 1
        elif msg.voice:
            file_data = await download_file(msg.voice.file_id, bot)
            if file_data:
                discord_files.append(("voice", file_data, "voice.ogg"))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "голосовое"))
                stats['files_skipped'] += 1
        elif msg.sticker:
            file_data = await download_file(msg.sticker.file_id, bot)
            if file_data:
                ext = "tgs" if (msg.sticker.is_animated or msg.sticker.is_video) else "webp"
                discord_files.append(("sticker", file_data, f"sticker.{ext}"))
                stats['files_processed'] += 1
            else:
                link = get_message_link(msg) or get_forward_link(msg)
                skipped_links.append((link, "стикер"))
                stats['files_skipped'] += 1

    if discord_files:
        for i in range(0, len(discord_files), 10):
            chunk = discord_files[i:i+10]
            files = [discord.File(fp, filename=name) for _, fp, name in chunk]
            if i == 0:
                content = f"{PREFIX}{sender_info}\n{caption}" if caption else f"{PREFIX}{sender_info}"
                content = split_long_message(content)
                await channel.send(content=content, files=files)
            else:
                await channel.send(files=files)

    if skipped_links:
        lines = [f"**Пропущены файлы (превышен лимит {MAX_FILE_SIZE//(1024*1024)} МБ):**"]
        for link, typ in skipped_links:
            if link:
                lines.append(f"- [{typ}]({link})")
            else:
                lines.append(f"- {typ} (ссылка недоступна – личный чат)")
        await channel.send("\n".join(lines))

# ---------- Отправка одиночного сообщения ----------
async def send_single_message(msg, channel, context):
    sender_info = get_sender_info(msg.from_user)
    caption = msg.text or msg.caption or ""
    if msg.entities:
        caption = format_text_with_entities(caption, msg.entities)
    if msg.caption_entities:
        caption = format_text_with_entities(caption, msg.caption_entities)

    file_data = None
    file_type = None
    filename = None
    link = None

    if msg.photo:
        photo = msg.photo[-1]
        file_data = await download_file(photo.file_id, context.bot)
        file_type = "фото"
        filename = "photo.jpg"
        link = get_message_link(msg)
    elif msg.video:
        file_data = await download_file(msg.video.file_id, context.bot)
        file_type = "видео"
        filename = msg.video.file_name or "video.mp4"
        link = get_message_link(msg)
    elif msg.document:
        file_data = await download_file(msg.document.file_id, context.bot)
        file_type = "документ"
        filename = msg.document.file_name or "document.bin"
        link = get_message_link(msg)
    elif msg.audio:
        file_data = await download_file(msg.audio.file_id, context.bot)
        file_type = "аудио"
        filename = msg.audio.file_name or "audio.mp3"
        link = get_message_link(msg)
    elif msg.voice:
        file_data = await download_file(msg.voice.file_id, context.bot)
        file_type = "голосовое"
        filename = "voice.ogg"
        link = get_message_link(msg)
    elif msg.sticker:
        file_data = await download_file(msg.sticker.file_id, context.bot)
        file_type = "стикер"
        ext = "tgs" if (msg.sticker.is_animated or msg.sticker.is_video) else "webp"
        filename = f"sticker.{ext}"
        link = get_message_link(msg)

    if file_data:
        discord_file = discord.File(file_data, filename=filename)
        content = f"{PREFIX}{sender_info}\n{caption}" if caption else f"{PREFIX}{sender_info}"
        content = split_long_message(content)
        await channel.send(content=content, file=discord_file)
        stats['files_processed'] += 1
    elif link:
        text = f"{PREFIX}{sender_info}\n{caption}\n\n**[{file_type}]({link})** (файл слишком большой)"
        text = split_long_message(text)
        await channel.send(text)
        stats['files_skipped'] += 1
    elif msg.forward_origin:
        forward_link = get_forward_link(msg)
        if forward_link:
            text = f"{PREFIX}{sender_info}\n{caption}\n\n**[{file_type} из канала]({forward_link})** (файл слишком большой, ссылка на оригинал)"
        else:
            text = f"{PREFIX}{sender_info}\n{caption}\n\n**{file_type}** (файл слишком большой, ссылка недоступна)"
        text = split_long_message(text)
        await channel.send(text)
        stats['files_skipped'] += 1
    elif file_type:
        text = f"{PREFIX}{sender_info}\n{caption}\n\n**{file_type}** (файл слишком большой, ссылка недоступна для личного чата)"
        text = split_long_message(text)
        await channel.send(text)
        stats['files_skipped'] += 1
    elif caption:
        text = f"{PREFIX}{sender_info}\n{caption}"
        text = split_long_message(text)
        await channel.send(text)
    else:
        print("ℹ️ Пустое сообщение (возможно, другой тип)")
    stats['messages_processed'] += 1

# ---------- Команды Discord (префиксные) ----------
@discord_client.command()
async def pause(ctx):
    global bot_paused
    if ctx.author.guild_permissions.administrator:
        bot_paused = True
        await ctx.send("⏸ Пересылка сообщений приостановлена.")
        await send_log("Пересылка приостановлена", "INFO")
    else:
        await ctx.send("❌ Недостаточно прав.")

@discord_client.command()
async def resume(ctx):
    global bot_paused
    if ctx.author.guild_permissions.administrator:
        bot_paused = False
        await ctx.send("▶ Пересылка сообщений возобновлена.")
        await send_log("Пересылка возобновлена", "INFO")
    else:
        await ctx.send("❌ Недостаточно прав.")

@discord_client.command()
async def show_stats(ctx):
    embed = discord.Embed(title="📊 Статистика бота", color=0x00ff00)
    embed.add_field(name="Обработано сообщений", value=stats['messages_processed'])
    embed.add_field(name="Переслано файлов", value=stats['files_processed'])
    embed.add_field(name="Пропущено файлов", value=stats['files_skipped'])
    embed.add_field(name="Ошибок", value=stats['errors'])
    await ctx.send(embed=embed)

@discord_client.command()
async def ban(ctx, user_id: int):
    global IGNORE_USERS
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Недостаточно прав.")
        return
    if user_id not in IGNORE_USERS:
        IGNORE_USERS.append(user_id)
        await ctx.send(f"✅ Пользователь {user_id} добавлен в чёрный список.")
        await send_log(f"Забанен пользователь Telegram {user_id}", "INFO")
    else:
        await ctx.send(f"⚠️ Пользователь {user_id} уже в чёрном списке.")

@discord_client.command()
async def unban(ctx, user_id: int):
    global IGNORE_USERS
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Недостаточно прав.")
        return
    if user_id in IGNORE_USERS:
        IGNORE_USERS.remove(user_id)
        await ctx.send(f"✅ Пользователь {user_id} удалён из чёрного списка.")
        await send_log(f"Разбанен пользователь Telegram {user_id}", "INFO")
    else:
        await ctx.send(f"⚠️ Пользователь {user_id} не найден в чёрном списке.")

# ---------- Слеш-команды Discord (с deferred response) ----------
@discord_client.tree.command(name="pause", description="Приостановить пересылку")
async def slash_pause(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    global bot_paused
    if interaction.user.guild_permissions.administrator:
        bot_paused = True
        await interaction.followup.send("⏸ Пересылка сообщений приостановлена.")
        await send_log("Пересылка приостановлена (slash)", "INFO")
    else:
        await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)

@discord_client.tree.command(name="resume", description="Возобновить пересылку")
async def slash_resume(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    global bot_paused
    if interaction.user.guild_permissions.administrator:
        bot_paused = False
        await interaction.followup.send("▶ Пересылка сообщений возобновлена.")
        await send_log("Пересылка возобновлена (slash)", "INFO")
    else:
        await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)

@discord_client.tree.command(name="showstats", description="Показать статистику")
async def slash_show_stats(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = discord.Embed(title="📊 Статистика бота", color=0x00ff00)
    embed.add_field(name="Обработано сообщений", value=stats['messages_processed'])
    embed.add_field(name="Переслано файлов", value=stats['files_processed'])
    embed.add_field(name="Пропущено файлов", value=stats['files_skipped'])
    embed.add_field(name="Ошибок", value=stats['errors'])
    await interaction.followup.send(embed=embed)

@discord_client.tree.command(name="ban", description="Забанить пользователя Telegram по ID")
async def slash_ban(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=False)
    global IGNORE_USERS
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
        return
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.followup.send("❌ ID должен быть числом.", ephemeral=True)
        return
    if uid not in IGNORE_USERS:
        IGNORE_USERS.append(uid)
        await interaction.followup.send(f"✅ Пользователь {uid} добавлен в чёрный список.")
        await send_log(f"Забанен пользователь Telegram {uid} (slash)", "INFO")
    else:
        await interaction.followup.send(f"⚠️ Пользователь {uid} уже в чёрном списке.")

@discord_client.tree.command(name="unban", description="Разбанить пользователя Telegram")
async def slash_unban(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=False)
    global IGNORE_USERS
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Недостаточно прав.", ephemeral=True)
        return
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.followup.send("❌ ID должен быть числом.", ephemeral=True)
        return
    if uid in IGNORE_USERS:
        IGNORE_USERS.remove(uid)
        await interaction.followup.send(f"✅ Пользователь {uid} удалён из чёрного списка.")
        await send_log(f"Разбанен пользователь Telegram {uid} (slash)", "INFO")
    else:
        await interaction.followup.send(f"⚠️ Пользователь {uid} не найден в чёрном списке.")

# ---------- Основная логика Telegram ----------
async def telegram_to_discord(update: Update, context: TGContext):
    global stats, bot_paused
    if bot_paused:
        return

    msg = update.effective_message
    if not msg or msg.from_user.is_bot:
        return

    if msg.from_user.id in IGNORE_USERS:
        return

    if ALLOWED_USERS and msg.from_user.id not in ALLOWED_USERS:
        return

    chat = update.effective_chat
    if ALLOWED_CHATS:
        if chat.id not in ALLOWED_CHATS:
            return
    else:
        if chat.type != 'private':
            return

    text = msg.text or msg.caption or ""
    if any(word in text.lower() for word in IGNORE_WORDS):
        print(f"🚫 Игнорируем сообщение от {msg.from_user.id} (запрещённое слово): {text[:50]}...")
        return

    channel = discord_client.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print("❌ Канал Discord не найден")
        await send_log(f"Канал Discord {DISCORD_CHANNEL_ID} не найден", "ERROR")
        stats['errors'] += 1
        return

    media_group_id = msg.media_group_id
    if media_group_id:
        async with album_lock:
            if media_group_id not in pending_albums:
                pending_albums[media_group_id] = []
            pending_albums[media_group_id].append(msg)
        if len(pending_albums[media_group_id]) == 1:
            asyncio.create_task(album_timeout(media_group_id, channel))
        return

    await send_single_message(msg, channel, context)

async def album_timeout(media_group_id, channel):
    await asyncio.sleep(ALBUM_TIMEOUT)
    await send_album(media_group_id, channel)

# ---------- Команды Telegram ----------
async def tg_pause(update: Update, context: TGContext):
    global bot_paused
    if update.effective_user.id in ADMIN_IDS or not ADMIN_IDS:
        bot_paused = True
        await update.message.reply_text("⏸ Пересылка приостановлена.")
        await send_log("Пересылка приостановлена (Telegram)", "INFO")
    else:
        await update.message.reply_text("❌ Недостаточно прав.")

async def tg_resume(update: Update, context: TGContext):
    global bot_paused
    if update.effective_user.id in ADMIN_IDS or not ADMIN_IDS:
        bot_paused = False
        await update.message.reply_text("▶ Пересылка возобновлена.")
        await send_log("Пересылка возобновлена (Telegram)", "INFO")
    else:
        await update.message.reply_text("❌ Недостаточно прав.")

async def tg_stats(update: Update, context: TGContext):
    if update.effective_user.id in ADMIN_IDS or not ADMIN_IDS:
        text = (f"📊 Статистика:\n"
                f"Обработано сообщений: {stats['messages_processed']}\n"
                f"Переслано файлов: {stats['files_processed']}\n"
                f"Пропущено файлов: {stats['files_skipped']}\n"
                f"Ошибок: {stats['errors']}")
        await update.message.reply_text(text)
    else:
        await update.message.reply_text("❌ Недостаточно прав.")

async def tg_ban(update: Update, context: TGContext):
    global IGNORE_USERS
    if update.effective_user.id in ADMIN_IDS or not ADMIN_IDS:
        try:
            user_id = int(context.args[0])
            if user_id not in IGNORE_USERS:
                IGNORE_USERS.append(user_id)
                await update.message.reply_text(f"✅ Пользователь {user_id} забанен.")
                await send_log(f"Забанен пользователь Telegram {user_id} (команда /ban)", "INFO")
            else:
                await update.message.reply_text(f"⚠️ Пользователь {user_id} уже в бане.")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Использование: /ban <user_id>")
    else:
        await update.message.reply_text("❌ Недостаточно прав.")

async def tg_unban(update: Update, context: TGContext):
    global IGNORE_USERS
    if update.effective_user.id in ADMIN_IDS or not ADMIN_IDS:
        try:
            user_id = int(context.args[0])
            if user_id in IGNORE_USERS:
                IGNORE_USERS.remove(user_id)
                await update.message.reply_text(f"✅ Пользователь {user_id} разбанен.")
                await send_log(f"Разбанен пользователь Telegram {user_id} (команда /unban)", "INFO")
            else:
                await update.message.reply_text(f"⚠️ Пользователь {user_id} не в бане.")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Использование: /unban <user_id>")
    else:
        await update.message.reply_text("❌ Недостаточно прав.")

# ---------- Запуск ботов ----------
@discord_client.event
async def on_ready():
    print(f'✅ Discord бот {discord_client.user} подключился!')
    await send_log(f"Discord бот {discord_client.user} подключился", "INFO")
    try:
        synced = await discord_client.tree.sync()
        print(f"🔁 Синхронизировано {len(synced)} команд(ы).")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

async def run_discord():
    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    finally:
        await discord_client.close()

async def run_telegram():
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()

    # Сначала добавляем команды
    tg_app.add_handler(CommandHandler("pause", tg_pause))
    tg_app.add_handler(CommandHandler("resume", tg_resume))
    tg_app.add_handler(CommandHandler("stats", tg_stats))
    tg_app.add_handler(CommandHandler("ban", tg_ban))
    tg_app.add_handler(CommandHandler("unban", tg_unban))
    # Потом обработчик всех остальных сообщений (не команд)
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, telegram_to_discord))

    print("✅ Telegram бот запускается...")
    for attempt in range(5):
        try:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling()
            break
        except (TimedOut, OSError) as e:
            print(f"⚠️ Ошибка подключения (попытка {attempt+1}/5): {e}")
            await asyncio.sleep(5)
    else:
        print("❌ Не удалось подключиться к Telegram после 5 попыток.")
        return

    print("✅ Telegram бот успешно запущен и слушает сообщения.")
    await send_log("Telegram бот успешно запущен и слушает сообщения", "INFO")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

async def main():
    discord_task = asyncio.create_task(run_discord())
    telegram_task = asyncio.create_task(run_telegram())

    try:
        await asyncio.gather(discord_task, telegram_task)
    except KeyboardInterrupt:
        print("\n⏹ Остановка...")
        discord_task.cancel()
        telegram_task.cancel()
        await asyncio.gather(discord_task, telegram_task, return_exceptions=True)
    finally:
        print("Боты остановлены.")
        await send_log("Боты остановлены", "WARNING")

if __name__ == "__main__":
    keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass