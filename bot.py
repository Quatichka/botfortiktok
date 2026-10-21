"""Телеграм-бот: присылаешь ссылку — получаешь видео, нарезанное на шортсы."""
import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import banners
import shorts
import visuals

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "60"))
# Папка data/ на BotHost сохраняется между пересборками
DATA_DIR = Path(__file__).parent / "data"
FONT_PATH = shorts.resolve_font(os.getenv("FONT_PATH"), DATA_DIR)
# cookies.txt YouTube-аккаунта (формат Netscape) — без них YouTube часто не пускает сервер
COOKIES = Path(os.getenv("COOKIES_FILE") or DATA_DIR / "cookies.txt")
LOGIN_HELP = (
    "❌ YouTube не пускает сервер бота без входа в аккаунт («подтвердите, что вы не бот»).\n\n"
    "Нужен файл cookies YouTube: выгрузите его расширением «Get cookies.txt LOCALLY» "
    "(лучше с запасного аккаунта) и загрузите на BotHost в папку <code>data</code> "
    "под именем <code>cookies.txt</code>. Ссылки с TikTok и других сайтов работают и без этого."
)
STYLE = shorts.Style(
    text_color=os.getenv("TEXT_COLOR", "#9B30D0"),
    box_color=os.getenv("BOX_COLOR", "white"),
    part_label=os.getenv("PART_LABEL", "PART {n}"),
    fit=os.getenv("VIDEO_FIT", "crop"),  # crop | fit
    mirror=os.getenv("MIRROR", "1") != "0",
    speed=float(os.getenv("SPEED", "1.1")),
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


visuals.init(FONT_PATH)
_photo_ids: dict[str, str] = {}  # file_id уже загруженных картинок — не грузим их повторно


async def photo(key: str, render, *args):
    """Картинка для отправки: file_id, если она уже была в Telegram, иначе PNG."""
    if key in _photo_ids:
        return _photo_ids[key]
    data = await asyncio.to_thread(render, *args)
    return BufferedInputFile(data, filename=f"{key}.png")


def remember(key: str, sent: Message) -> None:
    if sent and sent.photo:
        _photo_ids[key] = sent.photo[-1].file_id


def companies_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in banners.COMPANIES:
        sound = any(banners.get_banner(b).probe(shorts.FFMPEG).has_audio for b in c.variants)
        kb.button(text=f"{'🔊' if sound else '🔇'} {c.name}", callback_data=f"co:{c.key}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="🚫 Без баннера", callback_data="bn:none"),
           InlineKeyboardButton(text="💰 Все расценки", callback_data="rates"))
    return kb.as_markup()


def variants_kb(company: banners.Company) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for bid, label in company.variants.items():
        sound = banners.get_banner(bid).probe(shorts.FFMPEG).has_audio
        kb.button(text=f"{'🔊' if sound else '🔇'} {label}", callback_data=f"bn:{bid}")
    kb.button(text="⬅️ Ко всем рекламодателям", callback_data="back")
    kb.adjust(1)
    return kb.as_markup()


GALLERY_CAPTION = (
    "🎬 <b>Видео получено!</b> Выбери баннер рекламодателя — или нарежь без рекламы.\n\n"
    "🔊 — баннер со звуком (видео встаёт на паузу)   🔇 — беззвучный, поверх видео\n"
    "<i>Ставка на плитке — за 100 000 просмотров</i>"
)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else width
    return "▰" * filled + "▱" * (width - filled) + f"  {round(100 * done / total) if total else 100}%"


@dp.message(CommandStart())
async def start(msg: Message):
    sent = await msg.answer_photo(
        await photo("welcome", visuals.welcome),
        caption=("👋 <b>Привет!</b> Я нарезаю видео на шортсы 9:16 и ставлю рекламные баннеры "
                 "по правилам рекламодателей — чтобы ролик подходил под выплату.\n\n"
                 "👇 <b>Просто пришли ссылку на видео</b>\n\n"
                 f"⏱ Длина части сейчас: <b>{user_seg.get(msg.from_user.id, SEGMENT_SECONDS)} с</b>"),
    )
    remember("welcome", sent)


@dp.message(Command("rates"))
async def rates(msg: Message):
    sent = await msg.answer_photo(await photo("rates", visuals.rates),
                                  caption="💰 <b>Расценки рекламодателей</b>\n<i>Ставки меняются — "
                                          "перед публикацией сверяйтесь с их каналами</i>")
    remember("rates", sent)


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
    await msg.answer(f"✅ Длина части: <b>{sec} с</b>. Тайминги баннеров пересчитаются под неё.")


@dp.message(F.text.regexp(URL_RE))
async def handle_link(msg: Message):
    url = URL_RE.search(msg.text).group(0)
    custom_title = msg.text.split("|", 1)[1].strip() if "|" in msg.text else None
    pending[msg.from_user.id] = (url, custom_title)
    sent = await msg.answer_photo(await photo("gallery", visuals.gallery), caption=GALLERY_CAPTION,
                                  reply_markup=companies_kb())
    remember("gallery", sent)


@dp.callback_query(F.data == "rates")
async def cb_rates(cb: CallbackQuery):
    await cb.answer()
    await rates(cb.message)


async def show(cb: CallbackQuery, key: str, render, args: tuple, caption: str,
               markup: InlineKeyboardMarkup | None) -> None:
    """Меняет картинку и подпись в том же сообщении, чтобы чат не засорялся."""
    media = InputMediaPhoto(media=await photo(key, render, *args), caption=caption)
    edited = await cb.message.edit_media(media, reply_markup=markup)
    if isinstance(edited, Message):
        remember(key, edited)


@dp.callback_query(F.data == "back")
async def cb_back(cb: CallbackQuery):
    await cb.answer()
    await show(cb, "gallery", visuals.gallery, (), GALLERY_CAPTION, companies_kb())


@dp.callback_query(F.data.startswith("co:"))
async def cb_company(cb: CallbackQuery, bot: Bot):
    company = banners.COMPANY_BY_KEY.get(cb.data[3:])
    if not company:
        await cb.answer("Неизвестный рекламодатель")
        return
    await cb.answer()
    if len(company.variants) == 1:
        await choose_banner(cb, bot, next(iter(company.variants)))
        return
    rates_lines = "\n".join(f"💰 {p}, {k}: <b>{r}</b>" for p, k, r, _ in company.rates[:3])
    await show(cb, f"variants_{company.key}", visuals.variants, (company.key,),
               f"<b>{company.name}</b> — {len(company.variants)} баннера\n\n{rates_lines}\n\n"
               "👇 Выбери баннер", variants_kb(company))


@dp.callback_query(F.data.startswith("bn:"))
async def cb_banner(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await choose_banner(cb, bot, cb.data[3:])


async def choose_banner(cb: CallbackQuery, bot: Bot, bid: str):
    uid = cb.from_user.id
    if uid not in pending:
        await cb.message.edit_caption(caption="⌛ Ссылка устарела — пришли её ещё раз.")
        return
    url, custom_title = pending.pop(uid)
    banner = banners.get_banner(bid).probe(shorts.FFMPEG) if bid != "none" else None
    if banner:
        seg = user_seg.get(uid, SEGMENT_SECONDS)
        # Макет: где, какого размера и когда встанет баннер в части такой длины
        await show(cb, f"place_{bid}_{seg}", visuals.placement, (bid, seg),
                   banners.banner_caption(banner), None)
    else:
        await cb.message.edit_caption(caption="🚫 <b>Без баннера</b>\n\n⏳ Начинаю нарезку…")
    await process(cb.message, bot, uid, url, custom_title, banner)


async def process(msg: Message, bot: Bot, uid: int, url: str, custom_title: str | None,
                  banner: banners.Banner | None):
    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await msg.answer("Подожди, ещё обрабатываю предыдущее видео.")
        return

    async with lock:
        status = await msg.answer("⏬ <b>Скачиваю видео…</b>")
        workdir = Path(tempfile.mkdtemp(prefix="shorts_"))
        try:
            src = await asyncio.to_thread(shorts.download, url, workdir, COOKIES)
            if custom_title:
                src.title = custom_title
            if src.duration > MAX_DURATION:
                await status.edit_text(f"❌ Видео длиннее {MAX_DURATION // 60} мин — слишком длинное.")
                return

            # Видео ускоряется, поэтому из исходника берём больше секунд: готовая часть
            # выйдет ровно той длины, что выбрал пользователь
            parts = shorts.plan_parts(src.duration, user_seg.get(uid, SEGMENT_SECONDS) * STYLE.speed)
            total = len(parts)
            head = (f"🎞 <b>{html.escape(src.title)}</b>\n"
                    f"⏱ {int(src.duration // 60)}:{int(src.duration % 60):02d} → {total} ч."
                    + (f" · 📢 {html.escape(banner.company.name)}" if banner else ""))
            await status.edit_text(f"{head}\n\n✂️ Готовлю нарезку…")

            async with render_sem:
                for i, (start, length) in enumerate(parts, 1):
                    await status.edit_text(f"{head}\n\n🎬 Рендер части {i} из {total}\n"
                                           f"{progress_bar(i - 1, total)}")
                    path = await asyncio.to_thread(
                        shorts.render_part, src, workdir, FONT_PATH,
                        i, total, start, length, STYLE, banner,
                    )
                    if path.stat().st_size > TG_LIMIT:
                        await msg.answer(f"Часть {i} больше 50 МБ — Telegram не даст отправить.")
                        continue
                    caption = f"🎬 <b>Часть {i}/{total}</b> · {html.escape(src.title)}"
                    if banner:
                        caption += f"\n📢 Баннер: {html.escape(banner.company.variants[banner.id])}"
                    await bot.send_video(
                        msg.chat.id, FSInputFile(path), caption=caption,
                        width=shorts.W, height=shorts.H,
                        duration=int(shorts.probe_duration(path)), supports_streaming=True,
                    )
                    path.unlink(missing_ok=True)
            await status.edit_text(f"{head}\n\n✅ <b>Готово!</b> {progress_bar(total, total)}\n"
                                   "Пришли следующую ссылку 👇")
        except shorts.LoginRequired:
            logging.warning("YouTube требует cookies: %s", url)
            await status.edit_text(LOGIN_HELP if not COOKIES.is_file() else
                                   "❌ YouTube не принял cookies — похоже, они устарели. "
                                   "Выгрузите <code>cookies.txt</code> заново и замените файл в папке "
                                   "<code>data</code> на BotHost.")
        except Exception as e:
            logging.exception("Ошибка обработки %s", url)
            await status.edit_text(f"❌ Ошибка: {str(e)[:300]}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


@dp.message()
async def fallback(msg: Message):
    await msg.answer("👇 Пришли ссылку на видео — TikTok, YouTube, Instagram и другие.\n"
                     "Как это работает — /start")


async def main():
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.aiohttp import AiohttpSession

    # Увеличенный таймаут — отправка видео может идти долго
    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=600),
              default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Как это работает"),
        BotCommand(command="rates", description="Расценки рекламодателей"),
        BotCommand(command="length", description="Длина части, например /length 45"),
    ])
    # Общие картинки рисуем заранее, чтобы первый пользователь не ждал
    asyncio.create_task(asyncio.to_thread(visuals.warm_up))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
