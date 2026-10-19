"""Телеграм-бот: присылаешь ссылку — получаешь видео, нарезанное на шортсы."""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import banners
import shorts

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "60"))
# Папка data/ на BotHost сохраняется между пересборками
FONT_PATH = shorts.resolve_font(os.getenv("FONT_PATH"), Path(__file__).parent / "data")
STYLE = shorts.Style(
    text_color=os.getenv("TEXT_COLOR", "#9B30D0"),
    box_color=os.getenv("BOX_COLOR", "white"),
    part_label=os.getenv("PART_LABEL", "PART {n}"),
    fit=os.getenv("VIDEO_FIT", "crop"),  # crop | fit
)
MAX_DURATION = int(os.getenv("MAX_DURATION", "3600"))
TG_LIMIT = 49 * 1024 * 1024  # лимит Bot API на отправку файла — 50 МБ

URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
user_seg: dict[int, int] = {}
user_locks: dict[int, asyncio.Lock] = {}
pending: dict[int, tuple[str, str | None]] = {}  # ссылка и своё название, пока выбирают баннер
render_sem = asyncio.Semaphore(1)  # одновременно рендерим одно видео


def companies_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in banners.COMPANIES:
        kb.button(text=c.name, callback_data=f"co:{c.key}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="🚫 Без баннера", callback_data="bn:none"),
           InlineKeyboardButton(text="💰 Все расценки", callback_data="rates"))
    return kb.as_markup()


def variants_kb(company: banners.Company) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for bid, label in company.variants.items():
        kb.button(text=label, callback_data=f"bn:{bid}")
    kb.button(text="⬅️ Назад", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer(
        "Пришли ссылку на видео (TikTok, YouTube, Instagram и др.) — "
        "я скачаю его и нарежу на части 9:16.\n\n"
        "Перед нарезкой можно выбрать рекламный баннер: бот вырежет у него фон и поставит "
        "по правилам рекламодателя (тайминги, размер, звук), чтобы ролик подходил под выплату.\n\n"
        "Своё название: <code>ссылка | Моё название</code>\n"
        f"Длина части: /length 60 (сейчас {user_seg.get(msg.from_user.id, SEGMENT_SECONDS)} с)\n"
        "Расценки рекламодателей: /rates"
    )


@dp.message(Command("rates"))
async def rates(msg: Message):
    await msg.answer(banners.rates_table())


@dp.message(Command("length"))
async def set_length(msg: Message, command: CommandObject):
    try:
        sec = int(command.args or "")
        if not 5 <= sec <= 180:
            raise ValueError
    except ValueError:
        await msg.answer("Укажи длину в секундах от 5 до 180, например: /length 60")
        return
    user_seg[msg.from_user.id] = sec
    await msg.answer(f"Готово, длина части: {sec} с")


@dp.message(F.text.regexp(URL_RE))
async def handle_link(msg: Message):
    url = URL_RE.search(msg.text).group(0)
    custom_title = msg.text.split("|", 1)[1].strip() if "|" in msg.text else None
    pending[msg.from_user.id] = (url, custom_title)
    await msg.answer("Выбери баннер рекламодателя или нарежь без баннера:", reply_markup=companies_kb())


@dp.callback_query(F.data == "rates")
async def cb_rates(cb: CallbackQuery):
    await cb.message.answer(banners.rates_table())
    await cb.answer()


@dp.callback_query(F.data == "back")
async def cb_back(cb: CallbackQuery):
    await cb.message.edit_text("Выбери баннер рекламодателя или нарежь без баннера:",
                               reply_markup=companies_kb())
    await cb.answer()


@dp.callback_query(F.data.startswith("co:"))
async def cb_company(cb: CallbackQuery, bot: Bot):
    company = banners.COMPANY_BY_KEY.get(cb.data[3:])
    if not company:
        await cb.answer("Неизвестный рекламодатель")
        return
    if len(company.variants) == 1:
        await cb.answer()
        await choose_banner(cb, bot, next(iter(company.variants)))
        return
    await cb.message.edit_text(f"{banners.company_card(company)}\n\nВыбери баннер:",
                               reply_markup=variants_kb(company))
    await cb.answer()


@dp.callback_query(F.data.startswith("bn:"))
async def cb_banner(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await choose_banner(cb, bot, cb.data[3:])


async def choose_banner(cb: CallbackQuery, bot: Bot, bid: str):
    uid = cb.from_user.id
    if uid not in pending:
        await cb.message.edit_text("Ссылка устарела — пришли её ещё раз.")
        return
    url, custom_title = pending.pop(uid)
    banner = banners.get_banner(bid) if bid != "none" else None
    if banner:
        await cb.message.edit_text(f"Баннер: <b>{banners.COMPANY_BY_BANNER[bid].variants[bid]}</b>\n\n"
                                   f"{banners.company_card(banner.company)}")
    else:
        await cb.message.edit_text("Нарезаю без баннера.")
    await process(cb.message, bot, uid, url, custom_title, banner)


async def process(msg: Message, bot: Bot, uid: int, url: str, custom_title: str | None,
                  banner: banners.Banner | None):
    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await msg.answer("Подожди, ещё обрабатываю предыдущее видео.")
        return

    async with lock:
        status = await msg.answer("⏬ Скачиваю…")
        workdir = Path(tempfile.mkdtemp(prefix="shorts_"))
        try:
            src = await asyncio.to_thread(shorts.download, url, workdir)
            if custom_title:
                src.title = custom_title
            if src.duration > MAX_DURATION:
                await status.edit_text("Видео слишком длинное.")
                return

            parts = shorts.plan_parts(src.duration, user_seg.get(uid, SEGMENT_SECONDS))
            total = len(parts)
            await status.edit_text(f"✂️ «{src.title}» — режу на {total} ч.")

            async with render_sem:
                for i, (start, length) in enumerate(parts, 1):
                    await status.edit_text(f"🎬 Рендер части {i}/{total}…")
                    path = await asyncio.to_thread(
                        shorts.render_part, src, workdir, FONT_PATH,
                        i, total, start, length, STYLE, banner,
                    )
                    if path.stat().st_size > TG_LIMIT:
                        await msg.answer(f"Часть {i} больше 50 МБ — Telegram не даст отправить.")
                        continue
                    caption = f"{src.title} — часть {i}/{total}"
                    if banner:
                        caption += f"\n📢 {banner.company.name}"
                    await bot.send_video(
                        msg.chat.id, FSInputFile(path), caption=caption,
                        width=shorts.W, height=shorts.H,
                        duration=int(shorts.probe_duration(path)), supports_streaming=True,
                    )
                    path.unlink(missing_ok=True)
            await status.edit_text(f"✅ Готово: {total} ч.")
        except Exception as e:
            logging.exception("Ошибка обработки %s", url)
            await status.edit_text(f"❌ Ошибка: {str(e)[:300]}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


@dp.message()
async def fallback(msg: Message):
    await msg.answer("Пришли ссылку на видео.")


async def main():
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession

    # Увеличенный таймаут — отправка видео может идти долго
    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=600),
              default=DefaultBotProperties(parse_mode="HTML"))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
