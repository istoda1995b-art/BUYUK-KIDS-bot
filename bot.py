"""
Buyuk Premium — Keshbek Telegram bot

Mijoz o'zi ro'yxatdan o'tadi, karta oladi, QR/shtrix-kod oladi,
balans va xaridlar tarixini ko'radi.

MUHIM: cashback serveri (main.py) bilan BIR XIL Postgres bazaga ulanadi.
Jadval tuzilmasi o'sha yerda yaratiladi — bu bot faqat o'qiydi va yozadi.
"""

import asyncio
import io
import logging
import os
import re
from datetime import datetime
from decimal import Decimal

import barcode
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from barcode.writer import ImageWriter
from sqlalchemy import BigInteger, Boolean, DateTime, Numeric, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ─────────────────────────── Sozlamalar ───────────────────────────


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"{name} muhit o'zgaruvchisi sozlanmagan")
    return value


def _async_dsn(raw: str) -> str:
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw


BOT_TOKEN = _env("BOT_TOKEN")
DATABASE_URL = _async_dsn(_env("DATABASE_URL"))
CARD_PREFIX = os.getenv("CARD_PREFIX", "29")
SHOP_NAME = os.getenv("SHOP_NAME", "Buyuk Premium")

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=5)
Session = async_sessionmaker(engine, expire_on_commit=False)

# ─────────────────── Modellar (main.py bilan bir xil) ───────────────────


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    card_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    client_name: Mapped[str] = mapped_column(String(200), default="")
    phone_number: Mapped[str] = mapped_column(String(30), default="", index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(30), default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal(0))
    cashback_percentage: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(1))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ─────────────────────────── Yordamchilar ───────────────────────────

PHONE_RE = re.compile(r"[^\d]")


def normalize_phone(raw: str) -> str:
    """Barcha ko'rinishlarni 9 xonali milliy raqamga keltiradi."""
    digits = PHONE_RE.sub("", raw or "")
    if digits.startswith("998"):
        digits = digits[3:]
    return digits[-9:] if len(digits) >= 9 else digits


def pretty_phone(digits: str) -> str:
    if len(digits) != 9:
        return digits
    return f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}"


def money(value: Decimal | float) -> str:
    return f"{int(value):,}".replace(",", " ")


def render_barcode(card_number: str) -> bytes | None:
    """EAN-13 shtrix-kod — oddiy lazerli skanerlar ham o'qiydi."""
    if len(card_number) != 13 or not card_number.isdigit():
        return None
    try:
        buf = io.BytesIO()
        barcode.get("ean13", card_number[:12], writer=ImageWriter()).write(
            buf, options={"module_height": 12.0, "font_size": 12, "quiet_zone": 4.0}
        )
        return buf.getvalue()
    except Exception:
        log.exception("Shtrix-kod yasashda xato")
        return None


# ─────────────────────────── Klaviatura ───────────────────────────

BTN_BALANCE = "💰 Балансим"
BTN_CARD = "🎫 Картам"
BTN_HISTORY = "🧾 Харидларим"
BTN_HELP = "ℹ️ Ёрдам"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_CARD)],
        [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Рақамимни юбориш", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# ─────────────────────────── Bot ───────────────────────────

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


async def find_by_chat(session: AsyncSession, chat_id: int) -> Client | None:
    return await session.scalar(
        select(Client).where(Client.telegram_chat_id == str(chat_id))
    )


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with Session() as session:
        client = await find_by_chat(session, message.chat.id)

    if client:
        await message.answer(
            f"Ассалому алайкум, {client.client_name}!\n\n"
            f"Карта: <code>{client.card_number}</code>\n"
            f"Баланс: <b>{money(client.balance)} сўм</b>",
            reply_markup=MAIN_KB,
        )
        return

    await message.answer(
        f"<b>{SHOP_NAME} — кешбек дастури</b>\n\n"
        "Ҳар харидингиздан балл йиғилади ва уни кейинги харидда "
        "тўлов сифатида ишлатасиз.\n\n"
        "Картангизни Telegram'га боғлаш учун телефон рақамингизни юборинг.\n"
        "<i>Карта касса орқали очилади.</i>",
        reply_markup=PHONE_KB,
    )


@dp.message(F.contact)
async def on_contact(message: Message) -> None:
    contact = message.contact

    # Boshqa odamning kontaktini yuborishga yo'l qo'ymaymiz
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Илтимос, ўз рақамингизни юборинг — тугма орқали.",
            reply_markup=PHONE_KB,
        )
        return

    phone = normalize_phone(contact.phone_number)
    if len(phone) != 9:
        await message.answer("Рақам нотўғри. Қайта уриниб кўринг.", reply_markup=PHONE_KB)
        return

    name = " ".join(filter(None, [contact.first_name, contact.last_name])).strip()
    chat_id = str(message.chat.id)

    async with Session() as session:
        # Karta FAQAT kassada ochiladi. Bot mavjud kartani Telegram'ga bog'laydi.
        client = await session.scalar(select(Client).where(Client.phone_number == phone))

        if client is None:
            await message.answer(
                "❗️ Бу рақам билан карта топилмади.\n\n"
                f"Дисконт карта <b>{SHOP_NAME}</b> кассасида очилади.\n"
                "Кассирга мурожаат қилиб, шу рақамни айтинг:\n"
                f"<b>{pretty_phone(phone)}</b>\n\n"
                "Карта очилгандан сўнг бу ерга қайтиб /start босинг.",
                reply_markup=PHONE_KB,
            )
            log.info("Karta topilmadi, telefon=%s", phone)
            return

        if client.telegram_chat_id and client.telegram_chat_id != chat_id:
            log.info("Karta %s boshqa Telegram'ga qayta bog'landi", client.card_number)

        client.telegram_chat_id = chat_id
        if not client.client_name:
            client.client_name = name
        await session.commit()

    log.info("Karta bog'landi: %s", client.card_number)
    await message.answer(
        "✅ Картангиз топилди!\n\n"
        f"Карта: <code>{client.card_number}</code>\n"
        f"Баланс: <b>{money(client.balance)} сўм</b>",
        reply_markup=MAIN_KB,
    )
    await send_card(message, client)


@dp.message(F.text == BTN_BALANCE)
async def on_balance(message: Message) -> None:
    async with Session() as session:
        client = await find_by_chat(session, message.chat.id)

    if not client:
        await message.answer("Аввал рўйхатдан ўтинг: /start")
        return

    await message.answer(
        f"💰 Балансингиз: <b>{money(client.balance)} сўм</b>\n\n"
        f"Карта: <code>{client.card_number}</code>\n"
        f"Телефон: {pretty_phone(client.phone_number)}"
    )


@dp.message(F.text == BTN_CARD)
async def on_card(message: Message) -> None:
    async with Session() as session:
        client = await find_by_chat(session, message.chat.id)

    if not client:
        await message.answer("Аввал рўйхатдан ўтинг: /start")
        return

    await send_card(message, client)


async def send_card(message: Message, client: Client) -> None:
    caption = (
        f"🎫 <b>{client.client_name}</b>\n"
        f"Карта: <code>{client.card_number}</code>\n"
        f"Баланс: <b>{money(client.balance)} сўм</b>\n\n"
        "Кассада шу кодни кўрсатинг."
    )

    png = render_barcode(client.card_number)
    if png:
        await message.answer_photo(
            BufferedInputFile(png, filename="card.png"), caption=caption
        )
    else:
        # Shtrix-kod yasalmasa ham mijoz raqamni aytib bera olsin
        await message.answer(caption)


@dp.message(F.text == BTN_HISTORY)
async def on_history(message: Message) -> None:
    async with Session() as session:
        client = await find_by_chat(session, message.chat.id)
        if not client:
            await message.answer("Аввал рўйхатдан ўтинг: /start")
            return

        rows = (
            await session.execute(
                text(
                    """
                    SELECT created_at, purchase_amount, cashback_earned, cashback_spent
                    FROM transactions
                    WHERE client_id = :cid
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                ),
                {"cid": client.id},
            )
        ).all()

    if not rows:
        await message.answer("Ҳозирча харидлар йўқ.")
        return

    lines = ["🧾 <b>Сўнгги харидлар</b>\n"]
    for created, purchase, earned, spent in rows:
        line = f"{created:%d.%m.%Y %H:%M} — {money(purchase)} сўм"
        if earned:
            line += f"\n   ➕ {money(earned)} балл"
        if spent:
            line += f"\n   ➖ {money(spent)} балл ишлатилди"
        lines.append(line)

    lines.append(f"\n💰 Жорий баланс: <b>{money(client.balance)} сўм</b>")
    await message.answer("\n".join(lines))


@dp.message(F.text == BTN_HELP)
async def on_help(message: Message) -> None:
    await message.answer(
        f"<b>{SHOP_NAME} кешбек дастури</b>\n\n"
        "• Ҳар харидингиздан балл йиғилади\n"
        "• Харид суммаси қанча катта бўлса, фоиз шунча юқори\n"
        "• Йиғилган баллни кейинги харидда тўлов сифатида ишлатасиз\n\n"
        "Карта кассада очилади. Кассада «Картам» тугмасидаги кодни кўрсатинг.\n\n"
        "Савол бўлса — маъмуриятга мурожаат қилинг."
    )


@dp.message()
async def fallback(message: Message) -> None:
    async with Session() as session:
        client = await find_by_chat(session, message.chat.id)

    if client:
        await message.answer("Қуйидаги тугмалардан фойдаланинг:", reply_markup=MAIN_KB)
    else:
        await message.answer("Рўйхатдан ўтиш учун: /start")


async def main() -> None:
    log.info("Bot ishga tushdi")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
