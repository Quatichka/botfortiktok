"""Картинки для бота: приветствие, галерея баннеров, макет вставки, таблица расценок.

Всё рисуется Pillow из самих файлов баннеров, поэтому превью всегда совпадает с тем,
что окажется в видео. Готовые картинки кэшируются в памяти.
"""
import io
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import banners
import shorts

BG = (18, 16, 28)
CARD = (32, 28, 48)
CARD_HI = (44, 38, 66)
ACCENT = (155, 48, 208)      # тот же фиолетовый, что у подписей в видео
ACCENT_2 = (255, 196, 64)
TEXT = (245, 243, 250)
MUTED = (160, 152, 185)
GOOD = (88, 210, 140)

_font_path: Path | None = None


def init(font_path: Path) -> None:
    global _font_path
    _font_path = font_path


@lru_cache(maxsize=None)
def _f(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path), size)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, size: int, max_w: int) -> ImageFont.FreeTypeFont:
    while size > 14 and draw.textlength(text, font=_f(size)) > max_w:
        size -= 2
    return _f(size)


def _center(draw, xy_box, text, font, fill):
    x0, y0, x1, y1 = xy_box
    w = draw.textlength(text, font=font)
    asc, desc = font.getmetrics()
    draw.text((x0 + (x1 - x0 - w) / 2, y0 + (y1 - y0 - asc - desc) / 2), text, font=font, fill=fill)


def _pill(draw, x, y, text, font, fill, color=TEXT) -> int:
    """Плашка-ярлык; возвращает её ширину."""
    w = int(draw.textlength(text, font=font)) + font.size
    h = int(font.size * 1.6)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    _center(draw, (x, y, x + w, y + h), text, font, color)
    return w


def _gradient(w: int, h: int, top=(36, 22, 58), bottom=BG) -> Image.Image:
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return img


# --- Кадр баннера ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def banner_frame(bid: str) -> Image.Image:
    """Кадр из середины баннера с прозрачностью (верх файла — цвет, низ — маска)."""
    b = banners.get_banner(bid).probe(shorts.FFMPEG)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "f.png"
        subprocess.run([shorts.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{min(2.0, b.duration / 2):.2f}",
                        "-i", str(b.path), "-frames:v", "1", str(out)], check=True)
        im = Image.open(out).convert("RGB")
        im.load()
    w, h2 = im.size
    h = h2 // 2
    rgba = im.crop((0, 0, w, h)).convert("RGBA")
    rgba.putalpha(im.crop((0, h, w, h2)).convert("L"))
    bbox = rgba.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    return rgba.crop(bbox) if bbox else rgba


def _paste_fit(canvas: Image.Image, im: Image.Image, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    im = im.copy()
    im.thumbnail((x1 - x0, y1 - y0), Image.LANCZOS)
    canvas.paste(im, (x0 + (x1 - x0 - im.width) // 2, y0 + (y1 - y0 - im.height) // 2), im)


# --- Приветствие ----------------------------------------------------------------------------

@lru_cache(maxsize=None)
def welcome() -> bytes:
    W, H = 1280, 720
    img = _gradient(W, H)
    d = ImageDraw.Draw(img)
    d.text((70, 60), "Нарезка в Shorts", font=_f(64), fill=TEXT)
    d.text((70, 140), "+ рекламные баннеры с выплатой", font=_f(40), fill=ACCENT_2)

    steps = [
        ("1", "Пришли ссылку", ["TikTok, YouTube,", "Instagram и другие"]),
        ("2", "Выбери баннер", ["11 рекламодателей", "или без рекламы"]),
        ("3", "Получи шортсы", ["9:16, название сверху,", "PART N снизу"]),
    ]
    cw, gap, top = 350, 45, 250
    for i, (num, title, lines) in enumerate(steps):
        x = 70 + i * (cw + gap)
        d.rounded_rectangle((x, top, x + cw, top + 300), radius=28, fill=CARD)
        d.ellipse((x + 30, top + 30, x + 110, top + 110), fill=ACCENT)
        _center(d, (x + 30, top + 30, x + 110, top + 110), num, _f(48), TEXT)
        d.text((x + 30, top + 135), title, font=_f(38), fill=TEXT)
        for k, line in enumerate(lines):
            d.text((x + 30, top + 195 + k * 40), line, font=_f(28), fill=MUTED)
        if i < 2:
            ax = x + cw + 8
            d.polygon([(ax, top + 140), (ax + 28, top + 150), (ax, top + 160)], fill=MUTED)

    y = 600
    x = 70
    for text in ("/rates — расценки", "/length 45 — длина части", "ссылка | Название — своё название"):
        x += _pill(d, x, y, text, _f(26), CARD_HI) + 16
    return _png(img)


# --- Галерея рекламодателей -----------------------------------------------------------------

def _company_meta(c: banners.Company) -> tuple[bool, bool]:
    """(есть баннеры с озвучкой, есть беззвучные)."""
    sound = [banners.get_banner(b).probe(shorts.FFMPEG).has_audio for b in c.variants]
    return any(sound), not all(sound)


@lru_cache(maxsize=None)
def gallery() -> bytes:
    cols, tw, th, gap, pad = 4, 380, 300, 22, 40
    items = banners.COMPANIES
    rows = (len(items) + 1 + cols - 1) // cols
    W = pad * 2 + cols * tw + (cols - 1) * gap
    H = 170 + rows * (th + gap) + 30
    img = _gradient(W, H)
    d = ImageDraw.Draw(img)
    d.text((pad, 30), "Выбери баннер", font=_f(56), fill=TEXT)
    d.text((pad, 102), "кнопки — под картинкой  ·  ставки за 100 000 просмотров",
           font=_f(26), fill=MUTED)

    for i, c in enumerate(list(items) + [None]):
        x = pad + (i % cols) * (tw + gap)
        y = 170 + (i // cols) * (th + gap)
        d.rounded_rectangle((x, y, x + tw, y + th), radius=24, fill=CARD)
        if c is None:
            # Плитка «без баннера»
            d.ellipse((x + tw // 2 - 60, y + 40, x + tw // 2 + 60, y + 160), outline=MUTED, width=8)
            d.line((x + tw // 2 - 42, y + 142, x + tw // 2 + 42, y + 58), fill=MUTED, width=8)
            _center(d, (x, y + 180, x + tw, y + 230), "Без баннера", _f(34), TEXT)
            _center(d, (x, y + 230, x + tw, y + 270), "просто нарезка", _f(24), MUTED)
            continue
        d.rounded_rectangle((x + 14, y + 14, x + tw - 14, y + 174), radius=16, fill=(24, 21, 36))
        _paste_fit(img, banner_frame(next(iter(c.variants))), (x + 26, y + 22, x + tw - 26, y + 166))
        name_font = _fit_text(d, c.name, 32, tw - 40)
        d.text((x + 20, y + 186), c.name, font=name_font, fill=TEXT)
        d.text((x + 20, y + 228), c.rates[0][2], font=_fit_text(d, c.rates[0][2], 26, tw - 40), fill=ACCENT_2)
        has_sound, has_silent = _company_meta(c)
        px = x + 20
        if len(c.variants) > 1:
            px += _pill(d, px, y + 262, f"{len(c.variants)} баннера", _f(18), CARD_HI) + 8
        _pill(d, px, y + 262, "со звуком · пауза" if has_sound and not has_silent else
              "без звука" if not has_sound else "звук / без звука", _f(18), CARD_HI)
    return _png(img)


@lru_cache(maxsize=None)
def variants(company_key: str) -> bytes:
    c = banners.COMPANY_BY_KEY[company_key]
    items = list(c.variants.items())
    cols = 2 if len(items) > 1 else 1
    tw, th, gap, pad = 560, 380, 24, 40
    rows = (len(items) + cols - 1) // cols
    W = pad * 2 + cols * tw + (cols - 1) * gap
    H = 150 + rows * (th + gap) + 20
    img = _gradient(W, H)
    d = ImageDraw.Draw(img)
    d.text((pad, 40), c.name, font=_f(54), fill=TEXT)
    d.text((pad, 104), "выбери баннер кнопкой ниже", font=_f(26), fill=MUTED)
    for i, (bid, label) in enumerate(items):
        x = pad + (i % cols) * (tw + gap)
        y = 150 + (i // cols) * (th + gap)
        d.rounded_rectangle((x, y, x + tw, y + th), radius=24, fill=CARD)
        d.rounded_rectangle((x + 16, y + 16, x + tw - 16, y + 276), radius=16, fill=(24, 21, 36))
        _paste_fit(img, banner_frame(bid), (x + 30, y + 26, x + tw - 30, y + 266))
        d.text((x + 24, y + 292), label, font=_fit_text(d, label, 32, tw - 48), fill=TEXT)
        b = banners.get_banner(bid).probe(shorts.FFMPEG)
        _pill(d, x + 24, y + 336, f"{b.duration:.0f} с · " + ("со звуком, пауза видео" if b.pauses
                                                          else "без звука, поверх видео"),
              _f(20), CARD_HI)
    return _png(img)


# --- Макет вставки --------------------------------------------------------------------------

def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


@lru_cache(maxsize=64)
def placement(bid: str, seg: int) -> bytes:
    """Слева — кадр 9:16 с баннером ровно там и такого размера, как в видео.
    Справа — когда баннер появится в части длиной seg секунд."""
    b = banners.get_banner(bid).probe(shorts.FFMPEG)
    rule = b.rule

    # Кадр 1080x1920: фон, «видео», название и PART — тем же кодом, что и в рендере
    frame = Image.new("RGB", (shorts.W, shorts.H), (0, 0, 0))
    video = _gradient(shorts.W, shorts.VIDEO_H, top=(70, 76, 96), bottom=(38, 42, 58))
    vd = ImageDraw.Draw(video)
    for k in range(-shorts.VIDEO_H, shorts.W, 90):
        vd.line([(k, shorts.VIDEO_H), (k + shorts.VIDEO_H, 0)], fill=(60, 66, 86), width=24)
    _center(vd, (0, 0, shorts.W, shorts.VIDEO_H), "ВАШЕ ВИДЕО", _f(90), (120, 128, 152))
    frame.paste(video, (0, shorts.VIDEO_Y))
    with tempfile.TemporaryDirectory() as tmp:
        ov = Path(tmp) / "o.png"
        shorts.make_overlay("Название вашего видео", "PART 1", _font_path, shorts.Style(), ov)
        overlay = Image.open(ov).convert("RGBA")
        overlay.load()
    frame.paste(overlay, (0, 0), overlay)

    bw = int(shorts.W * rule.width) // 2 * 2
    bh = int(bw * b.h / b.w) // 2 * 2
    x = (shorts.W - bw) // 2
    y = {"center": (shorts.H - bh) // 2, "video_top": shorts.VIDEO_Y + 20,
         "video_bottom": shorts.VIDEO_Y + shorts.VIDEO_H - bh - 20}[rule.pos]
    scaled = _full_banner(bid).resize((bw, bh), Image.LANCZOS)
    frame.paste(scaled, (x, y), scaled)
    fd = ImageDraw.Draw(frame)
    fd.rectangle((x, y, x + bw, y + bh), outline=ACCENT_2, width=6)

    phone = frame.resize((450, 800), Image.LANCZOS)
    W, H = 1280, 900
    img = _gradient(W, H)
    shadow = Image.new("RGBA", (500, 850), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((10, 10, 490, 840), radius=50, fill=(0, 0, 0, 160))
    img.paste(shadow.filter(ImageFilter.GaussianBlur(14)), (35, 30), shadow.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((45, 35, 525, 865), radius=46, fill=(8, 8, 12))
    mask = Image.new("L", phone.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *phone.size), radius=30, fill=255)
    img.paste(phone, (60, 50), mask)

    # Правая колонка
    rx = 580
    label = b.company.variants[bid]
    d.text((rx, 50), label, font=_fit_text(d, label, 50, W - rx - 40), fill=TEXT)
    d.text((rx, 118), f"{b.company.rates[0][0]}: {b.company.rates[0][2]}", font=_f(30), fill=ACCENT_2)

    starts = [t for t in b.starts(seg) if t < seg]
    pos_txt = {"center": "по центру", "video_top": "вверху видео", "video_bottom": "внизу видео"}[rule.pos]
    facts = [
        ("Где", f"{pos_txt}, {int(rule.width * 100)}% ширины (жёлтая рамка)"),
        ("Когда", ", ".join(_fmt(t) for t in starts) + f"  (в части {seg} с)"),
        ("Длина", f"{b.duration:.1f} с, показов: {len(starts)}"),
        ("Звук", "только реклама, видео на паузе" if b.pauses else "звук ролика не трогаем"),
    ]
    fy = 190
    for k, v in facts:
        d.text((rx, fy), k, font=_f(26), fill=MUTED)
        d.text((rx + 120, fy - 2), v, font=_fit_text(d, v, 28, W - rx - 160), fill=TEXT)
        fy += 58

    # Шкала времени: видео, паузы и показы баннера
    total = seg + (len(starts) * b.duration if b.pauses else 0)
    tx0, tx1, ty = rx, W - 50, 500
    d.text((rx, ty - 60), "Шкала части", font=_f(32), fill=TEXT)
    d.rounded_rectangle((tx0, ty, tx1, ty + 56), radius=14, fill=(70, 66, 96))
    sx = lambda t: tx0 + (tx1 - tx0) * t / total
    for t in starts:
        x0, x1 = sx(t), sx(min(total, t + b.duration))
        d.rounded_rectangle((x0, ty - 6, max(x0 + 8, x1), ty + 62), radius=10,
                            fill=ACCENT if b.pauses else ACCENT_2)
    d.text((tx0, ty + 72), "0:00", font=_f(22), fill=MUTED)
    end = _fmt(total)
    d.text((tx1 - d.textlength(end, font=_f(22)), ty + 72), end, font=_f(22), fill=MUTED)

    ly = ty + 130
    d.rounded_rectangle((rx, ly, rx + 36, ly + 28), radius=6, fill=(70, 66, 96))
    d.text((rx + 50, ly - 2), "ваше видео", font=_f(24), fill=MUTED)
    d.rounded_rectangle((rx + 260, ly, rx + 296, ly + 28), radius=6, fill=ACCENT if b.pauses else ACCENT_2)
    d.text((rx + 310, ly - 2), "реклама, видео на паузе" if b.pauses else "баннер поверх видео",
           font=_f(24), fill=MUTED)

    note = (f"Часть станет длиннее на {len(starts) * b.duration:.0f} с из-за паузы"
            if b.pauses else "Длина части не меняется")
    d.rounded_rectangle((rx, 720, W - 50, 800), radius=18, fill=CARD)
    _center(d, (rx, 720, W - 50, 800), note, _fit_text(d, note, 28, W - rx - 90), GOOD)
    return _png(img)


@lru_cache(maxsize=None)
def _full_banner(bid: str) -> Image.Image:
    """Баннер целиком, без обрезки прозрачных полей — для точного масштаба на макете."""
    b = banners.get_banner(bid).probe(shorts.FFMPEG)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "f.png"
        subprocess.run([shorts.FFMPEG, "-y", "-loglevel", "error", "-ss", f"{min(2.0, b.duration / 2):.2f}",
                        "-i", str(b.path), "-frames:v", "1", str(out)], check=True)
        im = Image.open(out).convert("RGB")
        im.load()
    w, h2 = im.size
    rgba = im.crop((0, 0, w, h2 // 2)).convert("RGBA")
    rgba.putalpha(im.crop((0, h2 // 2, w, h2)).convert("L"))
    return rgba


# --- Таблица расценок -----------------------------------------------------------------------

@lru_cache(maxsize=None)
def rates() -> bytes:
    rows = banners.COMPANIES
    W, row_h, top = 1280, 74, 190
    H = top + len(rows) * row_h + 70 + 60 + len(banners.OTHER_RATES) * 44 + 60
    img = _gradient(W, H)
    d = ImageDraw.Draw(img)
    d.text((50, 40), "Расценки рекламодателей", font=_f(54), fill=TEXT)
    d.text((50, 108), "за 100 000 просмотров · данные на 19.10.2026, ставки часто меняются",
           font=_f(24), fill=MUTED)
    cols = [(50, "Рекламодатель"), (370, "Ставка"), (720, "Площадки / тип"), (1110, "Звук")]
    for x, t in cols:
        d.text((x, top - 40), t, font=_f(24), fill=MUTED)
    for i, c in enumerate(rows):
        y = top + i * row_h
        d.rounded_rectangle((36, y, W - 36, y + row_h - 10), radius=16, fill=CARD if i % 2 == 0 else CARD_HI)
        mid = y + (row_h - 10) // 2
        d.text((56, mid - 18), c.name, font=_fit_text(d, c.name, 30, 300), fill=TEXT)
        best = c.rates[0][2]
        d.text((370, mid - 18), best, font=_fit_text(d, best, 30, 330), fill=ACCENT_2)
        kind = f"{c.rates[0][0]} · {c.rates[0][1]}"
        d.text((720, mid - 14), kind, font=_fit_text(d, kind, 22, 370), fill=MUTED)
        has_sound, _ = _company_meta(c)
        _pill(d, 1110, mid - 16, "пауза" if has_sound else "поверх", _f(18),
              ACCENT if has_sound else (90, 84, 120))
    y = top + len(rows) * row_h + 40
    d.text((50, y), "Без готового баннера — запросить у рекламодателя", font=_f(30), fill=TEXT)
    y += 56
    for name, rate, note in banners.OTHER_RATES:
        d.text((56, y), name, font=_f(26), fill=TEXT)
        d.text((370, y), rate, font=_f(26), fill=ACCENT_2)
        d.text((560, y + 2), note, font=_fit_text(d, note, 22, W - 600), fill=MUTED)
        y += 44
    return _png(img)


def warm_up() -> None:
    """Рисует общие картинки заранее, чтобы первый пользователь не ждал."""
    welcome()
    gallery()
    rates()
