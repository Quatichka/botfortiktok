"""Скачивание видео и нарезка на вертикальные части 1080x1920 с подписями."""
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920


def ffmpeg_bin() -> str:
    """Системный ffmpeg, а если его нет — бинарник из пакета imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = ffmpeg_bin()

FONT_CSS = "https://fonts.googleapis.com/css2?family=Nunito:wght@800"


def resolve_font(path: str | None, cache_dir: Path) -> Path:
    """Шрифт из FONT_PATH, а если его нет — Nunito ExtraBold (как в примере),
    один раз скачанный с Google Fonts в cache_dir."""
    if path and Path(path).is_file():
        return Path(path)
    cached = cache_dir / "Nunito-ExtraBold.ttf"
    if cached.is_file():
        return cached
    import urllib.request
    css = urllib.request.urlopen(FONT_CSS, timeout=30).read().decode()
    url = re.search(r"url\((https://[^)]+\.ttf)\)", css).group(1)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(urllib.request.urlopen(url, timeout=60).read())
    return cached


@dataclass
class Downloaded:
    path: Path
    title: str
    duration: float


def clean_title(raw: str) -> str:
    """Убирает хэштеги, упоминания и лишние пробелы из названия."""
    text = re.sub(r"[#@]\S+", "", raw or "")
    text = re.sub(r"\s+", " ", text).strip(" -|·")
    return text or "Видео"


def download(url: str, workdir: Path) -> Downloaded:
    opts = {
        "outtmpl": str(workdir / "source.%(ext)s"),
        "format": "bv*[height<=1920]+ba/b[height<=1920]/bv*+ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(workdir.glob("source.*"))
    if not files:
        raise RuntimeError("yt-dlp ничего не скачал")
    path = files[0]
    title = clean_title(info.get("title") or info.get("description") or "")
    duration = float(info.get("duration") or probe_duration(path))
    return Downloaded(path, title, duration)


def probe_duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise RuntimeError("Не удалось определить длительность видео")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def plan_parts(duration: float, seg: int, min_tail: int = 10) -> list[tuple[float, float]]:
    """Список (начало, длина). Слишком короткий хвост приклеивается к последней части."""
    count = max(1, math.ceil(duration / seg))
    parts = [(i * seg, min(seg, duration - i * seg)) for i in range(count)]
    if len(parts) > 1 and parts[-1][1] < min_tail:
        tail = parts.pop()
        start, length = parts[-1]
        parts[-1] = (start, length + tail[1])
    return parts


@dataclass
class Style:
    text_color: str = "#9B30D0"   # фиолетовый, как в примере
    box_color: str = "white"
    part_label: str = "PART {n}"  # {n} — номер части, {total} — всего частей
    fit: str = "crop"             # crop — обрезать по бокам и заполнить кадр, fit — вписать целиком
    title_size: int = 66
    part_size: int = 78


# Макет 1080x1920 повторяет пример: название сверху, видео по центру, номер части под видео
VIDEO_Y, VIDEO_H = 350, 1210
MARGIN = 60


def wrap(text: str, font: ImageFont.FreeTypeFont, max_lines: int = 3) -> list[str]:
    """Переносит текст по словам, меряя реальную ширину строки в пикселях."""
    max_w = W - 2 * MARGIN
    lines: list[str] = []
    for word in text.split():
        if lines and font.getlength(f"{lines[-1]} {word}") <= max_w:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and font.getlength(lines[-1] + "…") > max_w:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _label(draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.FreeTypeFont,
           st: Style) -> None:
    """Строка текста по центру на плашке со скруглёнными углами."""
    size = font.size
    pad_x, pad_y = size // 4, size // 6
    tw = font.getlength(text)
    asc, desc = font.getmetrics()
    x = (W - tw) / 2
    draw.rounded_rectangle(
        (x - pad_x, y - pad_y, x + tw + pad_x, y + asc + desc + pad_y),
        radius=size // 3, fill=st.box_color,
    )
    draw.text((x, y), text, font=font, fill=st.text_color)


def make_overlay(title: str, label: str, font_path: Path, st: Style, out: Path) -> None:
    """Прозрачная PNG 1080x1920 с названием и номером части. Рисуем Pillow, а не
    ffmpeg drawtext — в статических сборках ffmpeg его часто нет."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(str(font_path), st.title_size)
    lines = wrap(title, title_font)
    line_h = int(st.title_size * 1.35)
    top = VIDEO_Y - 30 - len(lines) * line_h  # блок названия прижат к видео снизу
    for n, line in enumerate(lines):
        _label(draw, line, top + n * line_h, title_font, st)

    part_font = ImageFont.truetype(str(font_path), st.part_size)
    _label(draw, label, VIDEO_Y + VIDEO_H + 40, part_font, st)
    img.save(out)


def render_part(src: Downloaded, workdir: Path, font: Path, index: int, total: int,
                start: float, length: float, st: Style | None = None) -> Path:
    st = st or Style()
    overlay = workdir / f"overlay_{index:02d}.png"
    make_overlay(src.title, st.part_label.format(n=index, total=total), font, st, overlay)

    if st.fit == "crop":
        fg = f"scale={W}:{VIDEO_H}:force_original_aspect_ratio=increase,crop={W}:{VIDEO_H}"
    else:
        fg = f"scale={W}:{VIDEO_H}:force_original_aspect_ratio=decrease"
    graph = (
        f"color=black:s={W}x{H}:r=30[bg];[0:v]{fg},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:{VIDEO_Y}+({VIDEO_H}-h)/2:shortest=1[base];"
        f"[base][1:v]overlay=0:0[v]"
    )

    out = workdir / f"part_{index:02d}.mp4"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src.path),
        "-i", str(overlay),
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", "5M", "-bufsize", "10M", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        # Граф фильтров длинный и забивает сообщение — вырезаем его
        err = res.stderr.replace(graph, "<filters>").strip()
        raise RuntimeError(f"ffmpeg: {err[:400]}")
    return out
