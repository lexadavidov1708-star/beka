import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "message_cache.json"
ADMIN_FILE = DATA_DIR / "admin_chat_id.txt"
MAX_CACHE = 5000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("beka_dialog")


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def load_admin():
    if ADMIN_FILE.exists():
        return int(ADMIN_FILE.read_text().strip())
    return None


def save_admin(chat_id):
    ADMIN_FILE.write_text(str(chat_id))


cache = load_cache()
admin_chat_id = load_admin()


def build_entry(msg):
    entry = {
        "text": msg.text or msg.caption or "",
        "from": msg.from_user.full_name if msg.from_user else "?",
        "chat_title": msg.chat.full_name or msg.chat.title or str(msg.chat.id),
        "media_type": None,
        "file_id": None,
    }
    if msg.photo:
        entry["media_type"] = "photo"
        entry["file_id"] = msg.photo[-1].file_id
    elif msg.video:
        entry["media_type"] = "video"
        entry["file_id"] = msg.video.file_id
    elif msg.video_note:
        entry["media_type"] = "video_note"
        entry["file_id"] = msg.video_note.file_id
    elif msg.voice:
        entry["media_type"] = "voice"
        entry["file_id"] = msg.voice.file_id
    elif msg.audio:
        entry["media_type"] = "audio"
        entry["file_id"] = msg.audio.file_id
    elif msg.document:
        entry["media_type"] = "document"
        entry["file_id"] = msg.document.file_id
    return entry


async def send_media(bot, chat_id, media_type, file_id, caption=None):
    if media_type == "photo":
        await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
    elif media_type == "video":
        await bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
    elif media_type == "video_note":
        if caption:
            await bot.send_message(chat_id=chat_id, text=caption)
        await bot.send_video_note(chat_id=chat_id, video_note=file_id)
    elif media_type == "voice":
        await bot.send_voice(chat_id=chat_id, voice=file_id, caption=caption)
    elif media_type == "audio":
        await bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption)
    elif media_type == "document":
        await bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
    elif caption:
        await bot.send_message(chat_id=chat_id, text=caption)


def trim_cache():
    if len(cache) > MAX_CACHE:
        for old_key in list(cache.keys())[: len(cache) - MAX_CACHE]:
            cache.pop(old_key, None)


MEDIA_EMOJI = {
    "photo": "📷",
    "video": "🎥",
    "video_note": "⭕",
    "voice": "🎤",
    "audio": "🎵",
    "document": "📄",
}

DIVIDER = "――――――――――――"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_chat_id
    if admin_chat_id is None:
        admin_chat_id = update.effective_chat.id
        save_admin(admin_chat_id)
        await update.message.reply_text(
            "👋 Готово! Теперь я буду присылать сюда:\n\n"
            "🗑️ удалённые сообщения\n"
            "✏️ изменённые сообщения\n"
            "📷 фото · 🎥 видео · ⭕ кружочки\n"
            "🎤 голосовые · 🎵 аудио · 📄 файлы\n\n"
            "из подключённых бизнес-чатов.\n\n"
            "⚠️ Одноразовые «исчезающие» медиа Telegram не передаёт ботам вообще — это ограничение платформы."
        )
    else:
        await update.message.reply_text("✅ Уже настроено.")


async def cache_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.business_message
    if msg is None:
        return
    key = f"{msg.chat.id}:{msg.message_id}"
    cache[key] = build_entry(msg)
    trim_cache()
    save_cache(cache)


async def handle_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_business_message
    if msg is None or admin_chat_id is None:
        return
    key = f"{msg.chat.id}:{msg.message_id}"
    old_entry = cache.get(key)
    new_entry = build_entry(msg)

    old_text = old_entry["text"] if old_entry and old_entry["text"] else "(пусто / не было в кэше)"
    new_text = new_entry["text"] or "(без текста)"

    body = (
        f"✏️ СООБЩЕНИЕ ИЗМЕНЕНО\n"
        f"{DIVIDER}\n"
        f"💬 Чат: {new_entry['chat_title']}\n"
        f"👤 От: {new_entry['from']}\n"
        f"{DIVIDER}\n\n"
        f"📝 Было:\n{old_text}\n\n"
        f"🔄 Стало:\n{new_text}"
    )

    cache[key] = new_entry
    trim_cache()
    save_cache(cache)

    await context.bot.send_message(chat_id=admin_chat_id, text=body)
    if new_entry["media_type"]:
        emoji = MEDIA_EMOJI.get(new_entry["media_type"], "")
        await send_media(
            context.bot, admin_chat_id, new_entry["media_type"], new_entry["file_id"],
            caption=f"{emoji} Текущее медиа",
        )


async def handle_deleted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deleted = update.deleted_business_messages
    if deleted is None or admin_chat_id is None:
        return
    for mid in deleted.message_ids:
        key = f"{deleted.chat.id}:{mid}"
        entry = cache.pop(key, None)
        save_cache(cache)
        if entry:
            media_tag = f"{MEDIA_EMOJI.get(entry['media_type'], '')} " if entry["media_type"] else ""
            text_part = entry["text"] or "(без текста)"
            caption = (
                f"🗑️ СООБЩЕНИЕ УДАЛЕНО {media_tag}\n"
                f"{DIVIDER}\n"
                f"💬 Чат: {entry['chat_title']}\n"
                f"👤 От: {entry['from']}\n"
                f"{DIVIDER}\n\n"
                f"{text_part}"
            )
            await send_media(context.bot, admin_chat_id, entry["media_type"], entry["file_id"], caption=caption)
        else:
            text = f"🗑️ Сообщение удалено, но в кэше его не было (id {mid}, чат {deleted.chat.id})."
            await context.bot.send_message(chat_id=admin_chat_id, text=text)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, cache_business_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, handle_edited))
    app.add_handler(TypeHandler(Update, handle_deleted))
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
