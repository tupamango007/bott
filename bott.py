import asyncio
import io
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes as TGContext
from telegram.error import BadRequest, TimedOut
from telegram.request import HTTPXRequest
import discord
from discord.ext import commands
from keep_alive import keep_alive
# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "8669124689:AAEvxjDjNJ-ja1fyT47-l_cAhStTbeypXfc"
DISCORD_BOT_TOKEN = "MTA3NDM5NzY0NDI1ODAyMTM3Ng.G7c6SI.iOFUdBWtC2Nw5JLnpxa_3liEwwjbO_jonSVS1M"
DISCORD_CHANNEL_ID = 1044303344811901070  # ID канала в дискорд

PREFIX = "📢 "
IGNORE_WORDS = ["спам", "реклама"]
IGNORE_USERS = []

ALLOWED_USERS = []          # пусто – все разрешены
ALLOWED_CHATS = []          # пусто – только личные сообщения

MAX_FILE_SIZE = 8 * 1024 * 1024        # 8 МБ
ALBUM_TIMEOUT = 0.5
# ====================================================

pending_albums = {}
album_lock = asyncio.Lock()

intents = discord.Intents.default()
discord_client = commands.Bot(command_prefix="!", intents=intents)

def get_forward_link(msg):
    """Возвращает ссылку на оригинал пересланного сообщения, если это возможно."""
    try:
        # Проверяем, есть ли информация о пересылке
        if not hasattr(msg, 'forward_origin') or not msg.forward_origin:
            return None

        # Разные типы источников пересылки
        origin = msg.forward_origin
        
        # Если переслано из канала или супергруппы
        if hasattr(origin, 'chat') and origin.chat:
            chat = origin.chat
            message_id = origin.message_id if hasattr(origin, 'message_id') else None
            
            if chat and message_id:
                # Формируем ссылку
                if chat.username:
                    # Публичный канал с username
                    return f"https://t.me/{chat.username}/{message_id}"
                else:
                    # Приватный канал (формат t.me/c/...)
                    chat_id = str(chat.id)
                    if chat_id.startswith('-100'):
                        chat_id = chat_id[4:]
                    elif chat_id.startswith('-'):
                        chat_id = chat_id[1:]
                    return f"https://t.me/c/{chat_id}/{message_id}"
        
        # Если переслано от пользователя (не даст публичной ссылки)
        return None
    except Exception as e:
        print(f"⚠️ Ошибка при получении ссылки на пересланное: {e}")
        return None

def get_sender_info(user):
    """Возвращает строку с информацией об отправителе (без ID)"""
    name = user.full_name
    username = f"(@{user.username})" if user.username else ""
    return f"**{name}** {username}".strip()

def get_message_link(msg):
    """Формирует ссылку на сообщение Telegram, если это возможно."""
    # Сначала пробуем встроенное свойство link (современные версии)
    try:
        if hasattr(msg, 'link') and msg.link:
            return msg.link
    except:
        pass

    # Ручное формирование для групп, супергрупп и каналов
    chat = msg.chat
    if chat.type in ['group', 'supergroup', 'channel']:
        chat_id = str(chat.id)
        # Убираем префикс -100 для супергрупп и каналов
        if chat_id.startswith('-100'):
            chat_id = chat_id[4:]
        elif chat_id.startswith('-'):
            chat_id = chat_id[1:]
        return f"https://t.me/c/{chat_id}/{msg.message_id}"
    # Для личных сообщений публичных ссылок не существует
    return None

async def download_file(file_id, bot):
    """Скачивает файл, если он не превышает MAX_FILE_SIZE, иначе возвращает None."""
    try:
        file = await bot.get_file(file_id)
    except BadRequest as e:
        if "File is too big" in str(e):
            print(f"⚠️ Файл {file_id} слишком большой (ошибка API), пропускаем.")
            return None
        else:
            raise

    if hasattr(file, 'file_size') and file.file_size > MAX_FILE_SIZE:
        print(f"⚠️ Файл {file_id} слишком большой ({file.file_size} байт), пропускаем.")
        return None

    file_bytes = await file.download_as_bytearray()
    if len(file_bytes) > MAX_FILE_SIZE:
        print(f"⚠️ Скачанный файл {file_id} превышает лимит, пропускаем.")
        return None

    return io.BytesIO(file_bytes)

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

    # Получаем объект бота из первого сообщения (он одинаков для всех в альбоме)
    bot = messages[0].get_bot()

    discord_files = []
    skipped_links = []  # элементы: (link or None, тип_файла)
    caption = None

    for msg in messages:
        if not caption:
            caption = msg.text or msg.caption or ""

        if msg.photo:
            photo = msg.photo[-1]
            file_data = await download_file(photo.file_id, bot)
            if file_data:
                discord_files.append(("photo", file_data, "photo.jpg"))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "фото"))
        elif msg.video:
            file_data = await download_file(msg.video.file_id, bot)
            if file_data:
                filename = msg.video.file_name or "video.mp4"
                discord_files.append(("video", file_data, filename))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "видео"))
        elif msg.document:
            file_data = await download_file(msg.document.file_id, bot)
            if file_data:
                filename = msg.document.file_name or "document.bin"
                discord_files.append(("document", file_data, filename))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "документ"))
        elif msg.audio:
            file_data = await download_file(msg.audio.file_id, bot)
            if file_data:
                filename = msg.audio.file_name or "audio.mp3"
                discord_files.append(("audio", file_data, filename))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "аудио"))
        elif msg.voice:
            file_data = await download_file(msg.voice.file_id, bot)
            if file_data:
                discord_files.append(("voice", file_data, "voice.ogg"))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "голосовое"))
        elif msg.sticker:
            file_data = await download_file(msg.sticker.file_id, bot)
            if file_data:
                ext = "tgs" if (msg.sticker.is_animated or msg.sticker.is_video) else "webp"
                discord_files.append(("sticker", file_data, f"sticker.{ext}"))
            else:
                link = get_message_link(msg)
                if link:
                    skipped_links.append((link, "стикер"))

    # Отправляем файлы, которые удалось скачать
    if discord_files:
        for i in range(0, len(discord_files), 10):
            chunk = discord_files[i:i+10]
            files = [discord.File(fp, filename=name) for _, fp, name in chunk]
            if i == 0:
                content = f"{PREFIX}{sender_info}\n{caption}" if caption else f"{PREFIX}{sender_info}"
                await channel.send(content=content, files=files)
            else:
                await channel.send(files=files)

    # Отправляем информацию о пропущенных файлах
    if skipped_links:
        lines = [f"**Пропущены файлы (превышен лимит {MAX_FILE_SIZE/1024/1024:.0f} МБ):**"]
        for link, typ in skipped_links:
            if link:
                lines.append(f"- [{typ}]({link})")
            else:
                lines.append(f"- {typ} (ссылка недоступна – личный чат)")
        await channel.send("\n".join(lines))

async def send_single_message(msg, channel, context):
    sender_info = get_sender_info(msg.from_user)
    caption = msg.text or msg.caption or ""
    file_data = None
    file_type = None
    filename = None
    link = None

    # Определяем тип и пытаемся скачать
    if msg.photo:
        photo = msg.photo[-1]
        file_data = await download_file(photo.file_id, context)
        file_type = "фото"
        filename = "photo.jpg"
        link = get_message_link(msg)
    elif msg.video:
        file_data = await download_file(msg.video.file_id, context)
        file_type = "видео"
        filename = msg.video.file_name or "video.mp4"
        link = get_message_link(msg)
    elif msg.document:
        file_data = await download_file(msg.document.file_id, context)
        file_type = "документ"
        filename = msg.document.file_name or "document.bin"
        link = get_message_link(msg)
    elif msg.audio:
        file_data = await download_file(msg.audio.file_id, context)
        file_type = "аудио"
        filename = msg.audio.file_name or "audio.mp3"
        link = get_message_link(msg)
    elif msg.voice:
        file_data = await download_file(msg.voice.file_id, context)
        file_type = "голосовое"
        filename = "voice.ogg"
        link = get_message_link(msg)
    elif msg.sticker:
        file_data = await download_file(msg.sticker.file_id, context)
        file_type = "стикер"
        ext = "tgs" if (msg.sticker.is_animated or msg.sticker.is_video) else "webp"
        filename = f"sticker.{ext}"
        link = get_message_link(msg)

    if file_data:
        # Файл успешно скачан
        discord_file = discord.File(file_data, filename=filename)
        content = f"{PREFIX}{sender_info}\n{caption}" if caption else f"{PREFIX}{sender_info}"
        await channel.send(content=content, file=discord_file)
    elif link:
        # Есть ссылка на текущее сообщение (группа/канал)
        text = f"{PREFIX}{sender_info}\n{caption}\n\n**[{file_type}]({link})** (файл слишком большой)"
        await channel.send(text)
    elif msg.forward_origin:
        # Попробуем получить ссылку на оригинал пересланного сообщения
        forward_link = get_forward_link(msg)
        if forward_link:
            text = f"{PREFIX}{sender_info}\n{caption}\n\n**[{file_type} из канала]({forward_link})** (файл слишком большой, ссылка на оригинал)"
            await channel.send(text)
        else:
            # Не удалось получить ссылку на оригинал
            text = f"{PREFIX}{sender_info}\n{caption}\n\n**{file_type}** (файл слишком большой, ссылка недоступна)"
            await channel.send(text)
    elif file_type:
        # Файл был, но ссылки нет (личный чат, не пересланный)
        text = f"{PREFIX}{sender_info}\n{caption}\n\n**{file_type}** (файл слишком большой, ссылка недоступна для личного чата)"
        await channel.send(text)
    elif caption:
        # Только текст
        await channel.send(f"{PREFIX}{sender_info}\n{caption}")
    else:
        print("ℹ️ Пустое сообщение (возможно, другой тип)")

async def telegram_to_discord(update: Update, context: TGContext):
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

# --- Discord события и запуск ---
@discord_client.event
async def on_ready():
    print(f'✅ Discord бот {discord_client.user} подключился!')

async def run_discord():
    try:
        await discord_client.start(DISCORD_BOT_TOKEN)
    finally:
        await discord_client.close()

async def run_telegram():
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).build()
    tg_app.add_handler(MessageHandler(filters.ALL, telegram_to_discord))

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

if __name__ == "__main__":
    keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass