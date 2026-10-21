"""Скачивание видео и нарезка на вертикальные части 1080x1920 с подписями."""
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yt_dlp
from PIL import Image, ImageDraw, ImageFont

from banners import Banner

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


class LoginRequired(RuntimeError):
    """YouTube требует подтвердить, что запрос не от бота (нужны cookies)."""


def download(url: str, workdir: Path, cookies: Path | None = None) -> Downloaded:
    opts = {
        "outtmpl": str(workdir / "source.%(ext)s"),
        # res — меньшая сторона кадра, так что 1080 подходит и для горизонтальных, и для
        # вертикальных видео. H.264 декодируется дешевле VP9/AV1 — важно при 1 ГБ RAM
        "format": "bv*+ba/b",
        "format_sort": ["res:1080", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    # YouTube часто не пускает IP дата-центров без входа в аккаунт — тогда нужны cookies
    if cookies and cookies.is_file():
        opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        if "confirm you" in str(e) and "not a bot" in str(e):
            raise LoginRequired(str(e)) from e
        raise
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
    mirror: bool = True           # отзеркалить видео по горизонтали (надписи и баннеры — нет)
    speed: float = 1.1            # ускорение видео и звука; тон звука не меняется


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


def has_audio(path: Path) -> bool:
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stderr
    return "Audio:" in err


def _banner_graph(banner: Banner, d: float, src_audio: bool) -> tuple[str, str, str, float]:
    """Фильтры для баннера. Возвращает (подготовка контента, наложение, звук, итоговая длина).

    Контент — [cv]/[ca]; наложение берёт [lay] и отдаёт [v]; звук отдаёт [a].
    В режиме паузы основное видео разрезается, кадр замирает на время баннера, а основной
    звук молчит — длина ролика растёт. Моменты показа из правил соблюдаются на итоговом
    ролике: второй баннер на 1:30 так и будет на 1:30, с учётом пауз перед ним.
    """
    b = banner.duration
    starts = sorted(t for t in banner.starts(d) if t < d)
    rule = banner.rule
    audio_in = "[ma]" if src_audio else f"anullsrc=r=44100:cl=stereo,atrim=0:{d:.3f},"

    if banner.pauses:
        # Где резать исходник: момент показа минус уже вставленные паузы
        cuts, prev = [], 0.0
        for i, t in enumerate(starts):
            prev = max(prev, min(d, t - i * b))
            cuts.append(prev)
        bounds = [0.0] + cuts + [d]
        # Куски исходника: [начало, конец, замирание в начале, замирание в конце].
        # Пустой кусок (баннер в самом начале или две паузы подряд) не режем — его
        # замирание переносим на соседний кусок
        pieces, carry = [], 0.0
        for k in range(len(bounds) - 1):
            a, z = bounds[k], bounds[k + 1]
            pad = b if k < len(starts) else 0.0
            if z - a < 0.04:
                if pieces:
                    pieces[-1][3] += pad
                else:
                    carry += pad
            else:
                pieces.append([a, z, carry, pad])
                carry = 0.0
        # Замирание в начале куска делаем отдельным кусочком из его первого кадра:
        # tpad со start_duration в статических сборках ffmpeg не удлиняет видео
        segs = []  # (начало, конец, замирание в конце, только кадр-заглушка)
        for a, z, sp, ep in pieces:
            if sp:
                segs.append((a, a + 0.04, sp, True))
            segs.append((a, z, ep, False))
        n = len(segs)
        parts_v = "".join(f"[s{i}]" for i in range(n))
        parts_a = "".join(f"[t{i}]" for i in range(n))
        pre = f"[mv]split={n}{parts_v};{audio_in}asplit={n}{parts_a};"
        concat = ""
        for i, (a, z, ep, still) in enumerate(segs):
            if still:
                # Один кадр, растянутый на длину баннера, и тишина той же длины
                v = (f"trim=start={a:.3f}:duration=0.04,setpts=PTS-STARTPTS,"
                     f"tpad=stop_mode=clone:stop_duration={ep:.3f},trim=duration={ep:.3f}")
                au = (f"atrim=start={a:.3f}:duration=0.04,asetpts=PTS-STARTPTS,volume=0,"
                      f"apad=whole_dur={ep:.3f}")
            else:
                v = f"trim={a:.3f}:{z:.3f},setpts=PTS-STARTPTS" + \
                    (f",tpad=stop_mode=clone:stop_duration={ep:.3f}" if ep else "")
                au = f"atrim={a:.3f}:{z:.3f},asetpts=PTS-STARTPTS" + (f",apad=pad_dur={ep:.3f}" if ep else "")
            pre += f"[s{i}]{v}[sv{i}];[t{i}]{au}[ta{i}];"
            concat += f"[sv{i}][ta{i}]"
        pre += f"{concat}concat=n={n}:v=1:a=1[cv][ca];"
        shown = [cuts[i] + i * b for i in range(len(starts))]
        total = d + len(starts) * b
    else:
        pre = f"[mv]null[cv];{audio_in}anull[ca];"
        shown = starts
        total = d

    # Баннер: верх кадра — цвет, низ — маска прозрачности (см. tools/prepare_banners.py)
    bw = int(W * rule.width) // 2 * 2
    bh = int(bw * banner.h / banner.w) // 2 * 2
    x = (W - bw) // 2
    y = {"center": (H - bh) // 2,
         "video_top": VIDEO_Y + 20,
         "video_bottom": VIDEO_Y + VIDEO_H - bh - 20}[rule.pos]
    n = len(shown)
    copies = "".join(f"[bn{i}]" for i in range(n))
    lay = (f"[2:v]split[bc][bm];[bc]crop=iw:ih/2:0:0[bcol];"
           f"[bm]crop=iw:ih/2:0:ih/2,format=gray[bmask];"
           f"[bcol][bmask]alphamerge,scale={bw}:{bh},format=yuva420p,split={n}{copies};")
    cur = "[lay]"
    for i, t in enumerate(shown):
        nxt = "[v]" if i == n - 1 else f"[l{i}]"
        lay += (f"[bn{i}]setpts=PTS-STARTPTS+{t:.3f}/TB[bo{i}];"
                f"{cur}[bo{i}]overlay={x}:{y}:eof_action=pass{nxt};")
        cur = nxt

    # Звук: основной приглушаем на время баннера, звук баннера подмешиваем в нужные моменты
    windows = "+".join(f"between(t,{t:.3f},{t + b:.3f})" for t in shown)
    duck = f"volume=enable='{windows}':volume={rule.duck}" if rule.duck < 1 and banner.has_audio and not banner.pauses else "anull"
    aud = f"[ca]{duck}[main];"
    if banner.has_audio:
        acopies = "".join(f"[ba{i}]" for i in range(n))
        aud += f"[2:a]asplit={n}{acopies};"
        mix = "[main]"
        for i, t in enumerate(shown):
            ms = int(t * 1000)
            aud += f"[ba{i}]adelay={ms}|{ms}[bd{i}];"
            mix += f"[bd{i}]"
        aud += f"{mix}amix=inputs={n + 1}:duration=first:normalize=0[a]"
    else:
        aud += "[main]anull[a]"
    return pre, lay.rstrip(";"), aud, total


def render_part(src: Downloaded, workdir: Path, font: Path, index: int, total: int,
                start: float, length: float, st: Style | None = None,
                banner: Banner | None = None) -> Path:
    st = st or Style()
    overlay = workdir / f"overlay_{index:02d}.png"
    make_overlay(src.title, st.part_label.format(n=index, total=total), font, st, overlay)

    if st.fit == "crop":
        fg = f"scale={W}:{VIDEO_H}:force_original_aspect_ratio=increase,crop={W}:{VIDEO_H}"
    else:
        fg = f"scale={W}:{VIDEO_H}:force_original_aspect_ratio=decrease"
    layout = (
        f"color=black:s={W}x{H}:r=30[bg];[CV]{fg},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:{VIDEO_Y}+({VIDEO_H}-h)/2:shortest=1[base];"
        f"[base][1:v]overlay=0:0[OUT]"
    )
    # Основное видео: зеркало и ускорение. Дальше всё (баннеры, паузы, тайминги) считается
    # уже по ускоренному видео, поэтому длина части d — после ускорения
    src_audio = has_audio(src.path)
    speed = st.speed if st.speed > 0 else 1.0
    d = length / speed
    prep = f"[0:v]{'hflip,' if st.mirror else ''}setpts=(PTS-STARTPTS)/{speed}[mv];"
    if src_audio:
        prep += f"[0:a]asetpts=PTS-STARTPTS,atempo={speed}[ma];"

    banner_input: list[str] = []
    if banner:
        banner.probe(FFMPEG)
        pre, lay, aud, _ = _banner_graph(banner, d, src_audio)
        graph = f"{prep}{pre}{layout.replace('[CV]', '[cv]').replace('[OUT]', '[lay]')};{lay};{aud}"
        audio_map = ["-map", "[a]"]
        banner_input = ["-i", str(banner.path)]
    else:
        graph = prep + layout.replace("[CV]", "[mv]").replace("[OUT]", "[v]")
        audio_map = ["-map", "[ma]"] if src_audio else []

    out = workdir / f"part_{index:02d}.mp4"
    # Потоки и lookahead ограничены, чтобы ffmpeg укладывался в небольшую память хостинга
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "2", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src.path),
        "-i", str(overlay), *banner_input,
        "-filter_complex", graph, "-filter_complex_threads", "1",
        "-map", "[v]", *audio_map,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-threads", "2", "-x264-params", "rc-lookahead=10",
        "-maxrate", "5M", "-bufsize", "10M", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        # Граф фильтров длинный и забивает сообщение — вырезаем его
        err = res.stderr.replace(graph, "<filters>").strip()
        if res.returncode in (-9, 137) and not err:
            raise RuntimeError("ffmpeg убит системой — скорее всего, не хватило памяти")
        raise RuntimeError(f"ffmpeg (код {res.returncode}): {err[:400] or 'без сообщения'}")
    return out
