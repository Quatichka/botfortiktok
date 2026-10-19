"""Готовит баннеры для бота: вырезает фон и сжимает в компактный формат.

Запуск: python tools/prepare_banners.py <папка banners из архива>

На выходе banners/<id>.mp4 — H.264 с упакованной прозрачностью: верхняя половина кадра —
цвет, нижняя — маска (белое = видно). Такой файл весит 1–2 МБ, лежит в репозитории
и накладывается любой сборкой ffmpeg (alphamerge есть везде, в отличие от VP9 с альфой).
"""
import pathlib
import subprocess
import sys

import imageio_ffmpeg
from PIL import Image

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
OUT = pathlib.Path(__file__).resolve().parent.parent / "banners"
MAX_W = 1080

# id: (исходник, способ убрать фон, отдельная аудиодорожка или None)
#   "alpha" — у исходника уже есть прозрачность (ProRes 4444, CineForm, QuickTime RLE)
#   "key"   — однотонный фон, цвет берётся из угла кадра и вырезается хромакеем
#   "none"  — прямоугольный баннер со своим фоном, ставится как есть
SOURCES = {
    "playerok_tiktok": ("Playerok/MOV/TikTok/tiktok1.mov", "alpha", None),
    "funpay_games": ("FunPay/YtShorts + TikTok/Mov/ClipHub_games.mov", "alpha", None),
    "funpay_services": ("FunPay/YtShorts + TikTok/Mov/ClipHub_services.mov", "alpha", None),
    "musor_upgrade": ("Musor Drop/Musor Drop Uprade TT-Banner.webm", "none", None),
    "csdog_tiktok": ("CSDOG/TikTok NEW.mp4", "key", None),
    "csdog_reels": ("CSDOG/Reels NEW.mp4", "key", None),
    "csdog_shorts": ("CSDOG/Shorts NEW.mp4", "key", None),
    "csgorun_integration": ("CSGORUN/интеграция гиф+озвучка/баннер.mov", "alpha",
                            "CSGORUN/интеграция гиф+озвучка/озвучка.mp3"),
    "floatup_main": ("FloatUP/FLOATUP_MAIN.mp4", "none", None),
    "dogdrop_case": ("DogDrop/AlphaMOV_DD_Case_V3.mov", "alpha", "DogDrop/DogDrop_Case_v2.wav"),
    "dogdrop_upgrade": ("DogDrop/AlphaMov_DD_Upgrade_V3.mov", "alpha", "DogDrop/DogDrop_Upgrade_v2.wav"),
    # Зелёные Upgrade_Ru/Eng не используем: коробка кейса тоже зелёная и съедается хромакеем
    "topskin_cases": ("TopSkin/TopSkin Кейсы.mov", "alpha", None),
    "topskin_upgrade": ("TopSkin/TopSkin Апгрейд.mov", "alpha", None),
    "wondercase_main": ("WonderCase/WONDERCASE_greenn.mp4", "key", None),
    "wondercase_red": ("WonderCase/redlogoformat2.mp4", "none", None),
    "natria_vpn": ("NatriaVPN/NATRIA_VPN.mp4", "key", None),
    "getrush_3x1": ("GetRush/PIZDYK-3x1-FINAL.mp4", "key", None),
}


def corner_color(src: pathlib.Path, tmp: pathlib.Path) -> str:
    """Цвет фона — пиксель у левого верхнего угла кадра."""
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(src),
                    "-frames:v", "1", str(tmp)], check=True)
    r, g, b = Image.open(tmp).convert("RGB").getpixel((2, 2))
    tmp.unlink()
    return f"0x{r:02X}{g:02X}{b:02X}"


def prepare(bid: str, root: pathlib.Path) -> None:
    rel, mode, audio_rel = SOURCES[bid]
    src = root / rel
    scale = f"fps=30,scale='min(iw,{MAX_W})':-2:flags=lanczos"
    if mode == "alpha":
        rgba = f"[0:v]{scale},format=rgba"
    elif mode == "key":
        key = corner_color(src, OUT / f"_{bid}.png")
        g = int(key[4:6], 16)
        despill = ",despill=type=green" if g > 150 and g > int(key[2:4], 16) else ""
        rgba = f"[0:v]{scale},format=rgba,chromakey={key}:0.13:0.06{despill}"
        print(f"  {bid}: фон {key}")
    else:
        rgba = f"[0:v]{scale},format=rgba"
    graph = (f"{rgba},format=rgba,split[c][a];[a]alphaextract,format=yuv420p[m];"
             f"[c]format=yuv420p[cc];[cc][m]vstack,format=yuv420p[v]")

    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(src)]
    if audio_rel:
        cmd += ["-i", str(root / audio_rel)]
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    cmd += ["-map", "1:a" if audio_rel else "0:a?"]
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "24", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k"]
    if audio_rel:
        # Озвучка бывает короче видео: дополняем тишиной и режем по длине видео
        cmd += ["-af", "apad", "-shortest"]
    cmd += ["-movflags", "+faststart", str(OUT / f"{bid}.mp4")]
    subprocess.run(cmd, check=True)


def main() -> None:
    root = pathlib.Path(sys.argv[1])
    only = set(sys.argv[2:])
    OUT.mkdir(exist_ok=True)
    for bid in SOURCES:
        if only and bid not in only:
            continue
        prepare(bid, root)
        print(f"{bid}: {(OUT / f'{bid}.mp4').stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
