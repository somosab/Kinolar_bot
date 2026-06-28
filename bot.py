import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# === SOZLAMALAR ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) if os.getenv("ADMIN_ID", "").strip() else 0
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = "kinolar_bot"

# === MONGODB ===
def get_db():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured.")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    return client[DB_NAME]

def ensure_counter():
    db = get_db()
    db["counters"].update_one(
        {"_id": "movie_counter"},
        {"$setOnInsert": {"_id": "movie_counter", "value": 0}},
        upsert=True,
    )

def load_stats():
    db = get_db()
    ensure_counter()
    counter_doc = db["counters"].find_one({"_id": "movie_counter"})
    total = db["movies"].count_documents({})
    codes = [m["code"] for m in db["movies"].find({}, {"_id": 0, "code": 1}).sort("code", -1).limit(10)]
    return total, counter_doc.get("value", 0) if counter_doc else 0, codes

def save_movie(file_id: str) -> str:
    db = get_db()
    ensure_counter()
    counter_doc = db["counters"].find_one_and_update(
        {"_id": "movie_counter"},
        {"$inc": {"value": 1}},
        return_document=True,
        upsert=True,
    )
    code = str(counter_doc["value"]).zfill(4)
    db["movies"].insert_one({"code": code, "file_id": file_id})
    return code

def get_movie_by_code(code: str):
    db = get_db()
    return db["movies"].find_one({"code": code}, {"_id": 0, "code": 1, "file_id": 1})

def delete_movie_by_code(code: str) -> bool:
    db = get_db()
    result = db["movies"].delete_one({"code": code})
    return result.deleted_count > 0

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
waiting_for_movie = set()
waiting_for_delete = set()

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"👋 Salom, <b>{update.effective_user.first_name}</b>!\n\n"
        f"🎬 Kino kodini yozing va to'liq kinoni oling!\n\n"
        f"Masalan: <b>0001</b>",
        parse_mode="HTML",
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sizda ruxsat yo'q!")
        return

    total, last_code, _ = load_stats()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kino yuklash", callback_data="upload_movie")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
        [InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="delete_movie")],
    ])

    await update.message.reply_text(
        f"🎛 <b>Admin Panel</b>\n\n"
        f"📁 Jami kinolar: <b>{total}</b>\n"
        f"🔢 Oxirgi kod: <b>{str(last_code).zfill(4)}</b>\n\n"
        f"Nima qilmoqchisiz?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if query.from_user.id != ADMIN_ID:
        await query.answer("❌ Sizda ruxsat yo'q!", show_alert=True)
        return

    if query.data == "upload_movie":
        waiting_for_movie.add(query.from_user.id)
        await query.message.reply_text("🎬 Kinoni yuboring (video fayl):")
        await query.answer()

    elif query.data == "stats":
        total, last_code, codes = load_stats()
        codes_text = "\n".join([f"• {c}" for c in codes]) if codes else "Hali yo'q"
        await query.message.reply_text(
            f"📊 <b>Statistika</b>\n\n"
            f"🎬 Jami kinolar: <b>{total}</b>\n\n"
            f"🔢 Oxirgi 10 ta kod:\n{codes_text}",
            parse_mode="HTML",
        )
        await query.answer()

    elif query.data == "delete_movie":
        waiting_for_delete.add(query.from_user.id)
        await query.message.reply_text("🗑 O'chirmoqchi bo'lgan kino kodini yozing (masalan: 0001):")
        await query.answer()

async def receive_movie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in waiting_for_movie:
        return
    if update.message is None or update.message.video is None:
        return

    code = save_movie(update.message.video.file_id)
    waiting_for_movie.discard(user_id)
    await update.message.reply_text(
        f"✅ Kino saqlandi!\n\n"
        f"🔢 Kod: <b>{code}</b>\n\n"
        f"Obunachilarga shu kodni bering!",
        parse_mode="HTML",
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    text = message.text.strip()
    user_id = update.effective_user.id

    # O'chirish rejimi
    if user_id in waiting_for_delete and re.fullmatch(r"\d{4}", text):
        waiting_for_delete.discard(user_id)
        code = text.zfill(4)
        if delete_movie_by_code(code):
            await message.reply_text(f"✅ <b>{code}</b> kodi o'chirildi!", parse_mode="HTML")
        else:
            await message.reply_text(f"❌ <b>{code}</b> kodi topilmadi!", parse_mode="HTML")
        return

    # Kino kodi
    if re.fullmatch(r"\d{4}", text):
        code = text.zfill(4)
        movie = get_movie_by_code(code)
        if movie:
            await message.reply_text("⏳ Kino yuklanmoqda...")
            await context.bot.send_video(
                chat_id=message.chat_id,
                video=movie["file_id"],
                caption=f"🎬 Kino kodi: <b>{code}</b>\n\n@tomosha_kodi_bot",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "❌ Bunday kod topilmadi!\n\n"
                "Kodni to'g'ri yozdingizmi? Masalan: <b>0001</b>",
                parse_mode="HTML",
            )

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured.")
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured.")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("somosab", admin_panel))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.VIDEO, receive_movie))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()