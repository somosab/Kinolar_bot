import asyncio
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# === SOZLAMALAR ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) if os.getenv("ADMIN_ID", "").strip() else 0
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = "kinolar_bot"
BOT_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{35,}$")


def validate_bot_token(token: str) -> str:
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Please add it to .env or set the BOT_TOKEN environment variable."
        )
    if not BOT_TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError(
            "BOT_TOKEN format looks invalid. Create a new bot with @BotFather and copy the full token."
        )
    return token

# === MONGODB ===
def get_db():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured. Please add it to .env")
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')  # Test connection
        return client[DB_NAME]
    except Exception as e:
        raise RuntimeError(f"MongoDB connection failed: {e}")


def get_movies_collection():
    db = get_db()
    return db["movies"]


def get_counters_collection():
    db = get_db()
    return db["counters"]


def ensure_counter():
    counters = get_counters_collection()
    counters.update_one(
        {"_id": "movie_counter"},
        {"$setOnInsert": {"_id": "movie_counter", "value": 0}},
        upsert=True,
    )


def load_db():
    movies_collection = get_movies_collection()
    ensure_counter()
    
    counters_collection = get_counters_collection()
    counter_doc = counters_collection.find_one({"_id": "movie_counter"})
    movies_cursor = movies_collection.find({}, {"_id": 0, "code": 1, "file_id": 1})

    movies = {
        movie["code"]: {"file_id": movie["file_id"], "code": movie["code"]}
        for movie in movies_cursor
    }

    return {
        "movies": movies,
        "last_code": counter_doc.get("value", 0) if counter_doc else 0,
    }


def save_movie(file_id: str) -> str:
    movies_collection = get_movies_collection()
    counters_collection = get_counters_collection()
    ensure_counter()

    counter_doc = counters_collection.find_one_and_update(
        {"_id": "movie_counter"},
        {"$inc": {"value": 1}},
        return_document=True,
        upsert=True,
    )
    code = str(counter_doc["value"]).zfill(4)
    movies_collection.insert_one({"code": code, "file_id": file_id})
    return code


def get_movie_by_code(code: str):
    movies_collection = get_movies_collection()
    return movies_collection.find_one({"code": code}, {"_id": 0, "code": 1, "file_id": 1})


# === BOT ===
bot = None
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
logging.basicConfig(level=logging.INFO)

# === STATES ===
class AdminStates(StatesGroup):
    waiting_for_movie = State()


# === ADMIN PANEL ===
@dp.message(Command("somosab"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Sizda ruxsat yo'q!")
        return

    db = load_db()
    total = len(db["movies"])
    last_code = str(db["last_code"]).zfill(4)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kino yuklash", callback_data="upload_movie")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="stats")],
        [InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="delete_movie")],
    ])

    await message.answer(
        f"🎛 <b>Admin Panel</b>\n\n"
        f"📁 Jami kinolar: <b>{total}</b>\n"
        f"🔢 Oxirgi kod: <b>{last_code}</b>\n\n"
        f"Nima qilmoqchisiz?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# === KINO YUKLASH ===
@dp.callback_query(F.data == "upload_movie")
async def upload_movie(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🎬 Kinoni yuboring (video fayl):")
    await state.set_state(AdminStates.waiting_for_movie)
    await callback.answer()


@dp.message(AdminStates.waiting_for_movie, F.video)
async def receive_movie(message: types.Message, state: FSMContext):
    code = save_movie(message.video.file_id)

    await state.clear()
    await message.answer(
        f"✅ Kino saqlandi!\n\n"
        f"🔢 Kod: <b>{code}</b>\n\n"
        f"Obunachilarga shu kodni bering!",
        parse_mode="HTML",
    )


# === STATISTIKA ===
@dp.callback_query(F.data == "stats")
async def show_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    db = load_db()
    total = len(db["movies"])
    codes = list(db["movies"].keys())
    codes_text = "\n".join([f"• {c}" for c in codes[-10:]]) if codes else "Hali yo'q"

    await callback.message.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"🎬 Jami kinolar: <b>{total}</b>\n\n"
        f"🔢 Oxirgi 10 ta kod:\n{codes_text}",
        parse_mode="HTML",
    )
    await callback.answer()


# === KINO O'CHIRISH ===
@dp.callback_query(F.data == "delete_movie")
async def delete_movie_prompt(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("🗑 O'chirmoqchi bo'lgan kino kodini yozing (masalan: 0001):")
    await callback.answer()


# === FOYDALANUVCHI KODNI YOZGANDA ===
@dp.message(F.text.regexp(r'^\d{4}$'))
async def send_movie(message: types.Message):
    code = message.text.zfill(4)
    movie = get_movie_by_code(code)

    if movie:
        await message.answer("⏳ Kino yuklanmoqda...")
        await bot.send_video(
            chat_id=message.chat.id,
            video=movie["file_id"],
            caption=f"🎬 Kino kodi: <b>{code}</b>\n\n@kulgili_videolar_uz",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Bunday kod topilmadi!\n\n"
            "Kodni to'g'ri yozdingizmi? Masalan: <b>0001</b>",
            parse_mode="HTML",
        )


# === START ===
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        f"👋 Salom, <b>{message.from_user.first_name}</b>!\n\n"
        f"🎬 Kino kodini yozing va to'liq kinoni oling!\n\n"
        f"Masalan: <b>0001</b>",
        parse_mode="HTML",
    )


# === ISHGA TUSHIRISH ===
async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not configured. Please add it to .env")

    global bot
    if bot is None:
        token = validate_bot_token(BOT_TOKEN)
        bot = Bot(token=token)

    try:
        await bot.get_me()
    except TelegramUnauthorizedError as exc:
        raise RuntimeError(
            "Telegram rejected the BOT_TOKEN. Create a new bot with @BotFather and update BOT_TOKEN in .env or your environment."
        ) from exc

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
