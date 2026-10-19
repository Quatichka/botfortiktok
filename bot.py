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
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

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
render_sem = asyncio.Semaphore(1)  # одновременно рендерим одно видео


@dp.message(CommandStart())
async def start(msg: Message):
    await msg.answer(
        "Пришли ссылку на видео (TikTok, YouTube, Instagram и др.) — "
        "я скачаю его и нарежу на части 9:16.\n\n"
        "Сверху будет название, снизу — номер части.\n"
        "Своё название: <code>ссылка | Моё название</code>\n"
        f"Длина части: /length 60 (сейчас {user_seg.get(msg.from_user.id, SEGMENT_SECONDS)} с)"
    )


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
async def handle_link(msg: Message, bot: Bot):
    url = URL_RE.search(msg.text).group(0)
    custom_title = msg.text.split("|", 1)[1].strip() if "|" in msg.text else None
    uid = msg.from_user.id
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
                        i, total, start, length, STYLE,
                    )
                    if path.stat().st_size > TG_LIMIT:
                        await msg.answer(f"Часть {i} больше 50 МБ — Telegram не даст отправить.")
                        continue
                    await bot.send_video(
                        msg.chat.id, FSInputFile(path),
                        caption=f"{src.title} — часть {i}/{total}",
                        width=shorts.W, height=shorts.H, duration=int(length),
                        supports_streaming=True,
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
