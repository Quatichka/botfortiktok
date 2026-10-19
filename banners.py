"""Рекламодатели, их баннеры, правила вставки и расценки.

Данные — из таблицы advertisers_banners.xlsx (проверено 19.10.2026). Ставки и правила
у рекламодателей меняются часто: перед публикацией сверяйтесь с их каналами.
"""
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BANNER_DIR = Path(__file__).parent / "banners"


@dataclass
class Rule:
    """Как ставить баннер.

    width — доля ширины экрана 1080; pos — center | video_top | video_bottom;
    timing — функция (длительность ролика, длительность баннера) -> моменты показа;
    pause — основное видео на время баннера встаёт на паузу (звучит только баннер);
      включена у всех: так звук баннера чётко слышен и он не перекрывает действие;
    duck — громкость основного звука, если паузу выключить (1 — не трогать).
    """
    timing: str
    width: float = 1.0
    pos: str = "center"
    pause: bool = True
    duck: float = 0.3


@dataclass
class Company:
    key: str
    name: str
    rule: Rule
    variants: dict[str, str]      # id баннера -> подпись кнопки
    rates: list[tuple[str, str, str, str]]  # площадка, тип контента, ставка, условия
    terms: list[str]
    how: str                      # как бот выполняет условия — показываем пользователю
    contacts: str = ""


# --- Тайминги. d — длительность ролика (части), b — длительность баннера, сек. ----------

def _fit(t: float, d: float, b: float) -> float:
    """Сдвигает показ так, чтобы баннер целиком помещался в ролик."""
    return max(0.0, min(t, d - b))


def _centered(d: float, b: float) -> list[float]:
    return [_fit(d / 2 - b / 2, d, b)]


def _starts_at_middle(d: float, b: float) -> list[float]:
    return [_fit(d / 2, d, b)]


def _per_minute_middle(d: float, b: float) -> list[float]:
    """По баннеру на каждую (начатую) минуту, в середине этой минуты."""
    out = []
    for k in range(max(1, math.ceil(d / 60))):
        chunk = min(60, d - k * 60)
        out.append(_fit(k * 60 + chunk / 2 - b / 2, d, b))
    return out


def _floatup(d: float, b: float) -> list[float]:
    # Видео от минуты: ровно 0:20, 1:20, 2:20…; для коротких правило не задано — середина
    if d < 60:
        return _centered(d, b)
    return [t for t in range(20, int(d), 60) if t + b <= d + 0.5] or _centered(d, b)


def _topskin(d: float, b: float) -> list[float]:
    # До 0:59 — ровно в середине; 1:00–1:59 — 0:30; 2:00–2:59 — 0:30 и 1:30; дальше +1 в минуту
    if d < 60:
        return _starts_at_middle(d, b)
    return [_fit(t, d, b) for t in range(30, int(d), 60)]


def _wondercase(d: float, b: float) -> list[float]:
    # До 59 с — 1 (в середине), 60–119 — 2, 120–179 — 3, дальше по одному на минуту
    n = 1 if d < 60 else int(d // 60) + 1
    return [_fit(d * (i + 0.5) / n - b / 2, d, b) for i in range(n)]


def _natria(d: float, b: float) -> list[float]:
    # До 2 мин — 1 раз в середине; от 2 мин — на 0:30 и 1:30
    return _starts_at_middle(d, b) if d < 120 else [30.0, 90.0]


def _dogdrop(d: float, b: float) -> list[float]:
    # В первые 30% хронометража, и так в каждой минуте
    out = []
    for k in range(max(1, math.ceil(d / 60))):
        chunk = min(60, d - k * 60)
        out.append(k * 60 + chunk * 0.2)
    return out


TIMINGS = {
    "centered": _centered, "per_minute": _per_minute_middle, "floatup": _floatup,
    "topskin": _topskin, "wondercase": _wondercase, "natria": _natria, "dogdrop": _dogdrop,
}


# --- Рекламодатели с готовыми баннерами --------------------------------------------------

COMPANIES: list[Company] = [
    Company(
        "playerok", "Playerok",
        Rule("centered", width=0.9),
        {"playerok_tiktok": "Playerok (TikTok)"},
        [("TikTok", "Любые темы по регламенту", "400 ₽ / 100 000", "от 50 000 просм.; подача до 30 дней"),
         ("Instagram Reels", "Любые темы по регламенту", "300 ₽ / 100 000", "от 50 000 просм.; подача до 21 дня")],
        ["YouTube закрыт с 01.09", "Запрещены темы: криминал, бандитские, армейские (в т.ч. фильмы/сериалы)",
         "Точные правила вставки — в регламенте рекламодателя"],
        "по центру, 90% ширины, один раз в середине ролика (точные правила — в регламенте Playerok)",
        "@playerokclips · бот @playerokclips_bot",
    ),
    Company(
        "funpay", "FunPay ClipHub",
        Rule("centered", width=0.95, pos="video_top"),
        {"funpay_games": "FunPay — игры", "funpay_services": "FunPay — сервисы"},
        [("YouTube Shorts", "Игровой контент", "700 ₽ / 100 000", ""),
         ("YouTube Shorts", "Minecraft / Roblox, аниме, дорамы", "500 ₽ / 100 000", ""),
         ("YouTube Shorts", "Смежный контент", "350 ₽ / 100 000", ""),
         ("YouTube Shorts", "Ролики от 500к (игровой / смежный / аниме)", "750 / 500 / 700 ₽", "учитывают удержание"),
         ("TikTok", "Игровой", "200 ₽ / 100 000", ""),
         ("TikTok", "Аниме / дорамы", "500 ₽ / 100 000", "")],
        ["Баннер на 90–100% ширины экрана с учётом слепых зон интерфейса",
         "Только новые баннеры; оплачивают ролики, прошедшие проверку в боте"],
        "95% ширины, вверху видео (вне слепых зон интерфейса), в середине ролика",
        "@funpay_cliphub · бот @funpay_cliphub_bot",
    ),
    Company(
        "musor", "Musor Drop",
        Rule("per_minute", width=0.9),
        {"musor_upgrade": "Musor Drop — апгрейд"},
        [("Все", "CS2-контент", "$70 / 100 000", "мин. 30 000 просм."),
         ("Все", "Не CS2-контент", "$10 / 100 000", ""),
         ("Все", "Креативы (даже с баннером)", "$10 / 100 000", "")],
        ["1 баннер на каждую минуту ролика", "Фильмы и сериалы не принимают с 12.10",
         "В канале жалобы на задержки выплат"],
        "по центру, 90% ширины, по одному в середине каждой минуты",
        "@musor_tiktok · правила @musorsupport",
    ),
    Company(
        "csdog", "CSDOG",
        Rule("centered", width=0.7),
        {"csdog_tiktok": "CSDOG — TikTok", "csdog_shorts": "CSDOG — Shorts", "csdog_reels": "CSDOG — Reels"},
        [("TikTok / Shorts / Reels", "Баннер", "от $100 / 100 000", ""),
         ("TikTok", "Формат «бутылочка»", "$150 / 100 000", "стартовая ставка")],
        ["Для каждой площадки свой файл баннера", "Только NEW-версия (без банворда «халява»)",
         "Точные правила вставки уточнить в чате проекта"],
        "по центру, 70% ширины, один раз в середине ролика",
        "@csdogTikTok · csdog-partners.com",
    ),
    Company(
        "csgorun", "CSGORUN",
        Rule("centered", width=1.0, duck=0.2),
        {"csgorun_integration": "CSGORUN — 5-сек. интеграция"},
        [("Все", "5-сек. интеграция, развлекательный", "$5 / 100 000", "от 2 000 просм."),
         ("Все", "5-сек. интеграция, CS2", "$40 / 100 000", ""),
         ("Все", "Нативный подвод", "$50 / 100 000", ""),
         ("Все", "Split-screen с интерфейсом", "$100 / 100 000", ""),
         ("Все", "Креатив про CSGORUN", "$200 / 100 000", "")],
        ["Интеграция хорошо заметна, с озвучкой", "Ссылку поставить в шапку профиля",
         "При 300к+ просмотров приложить статистику"],
        "на всю ширину, по центру, 5 секунд с озвучкой в середине ролика",
        "@Partnerrun · задания @TaskHunterRunBot",
    ),
    Company(
        "floatup", "FloatUP",
        Rule("floatup", width=1.0),
        {"floatup_main": "FloatUP"},
        [("TikTok / Instagram", "Не CS2, Surf/Bhop, CS2 частями", "$35 / 100 000", ""),
         ("TikTok / Instagram", "CS2: только прямой геймплей", "выше базовой", "уточнить")],
        ["Баннер не меньше 30% экрана, виден целиком", "Скорость баннера не менять",
         "Видео от 1 мин.: ровно на 0:20, 1:20, 2:20…", "Детский контент запрещён"],
        "на всю ширину (больше 30% экрана), по центру; у роликов от минуты — ровно на 0:20, 1:20…",
        "@floatup_TikTok",
    ),
    Company(
        "dogdrop", "DogDrop",
        Rule("dogdrop", width=1.0),
        {"dogdrop_case": "DogDrop — кейс", "dogdrop_upgrade": "DogDrop — апгрейд"},
        [("TikTok", "Баннер", "$300 / 100 000", "$30 за 10 000")],
        ["По центру; основное видео на паузе (звучит только баннер)",
         "В первые 30% хронометража; длиннее минуты — по баннеру на минуту",
         "Ускорение не больше 1,4x; ролик от 10 сек.; детский контент запрещён"],
        "по центру на всю ширину, в первых 30% каждой минуты",
        "@dogdrop_media",
    ),
    Company(
        "topskin", "TopSkin",
        Rule("topskin", width=1.0, duck=0.1),
        {"topskin_cases": "TopSkin — кейсы", "topskin_upgrade": "TopSkin — апгрейд"},
        [("TikTok / Shorts", "Баннер", "от $35 / 100 000", "вывод от $50")],
        ["Не меньше 25% экрана, звук баннера хорошо слышен (фон приглушить)",
         "До 0:59 — ровно в середине; 1:00–1:59 — на 0:30; 2:00–2:59 — 0:30 и 1:30",
         "Ролик от 10 сек.; не упоминать другие проекты"],
        "на всю ширину по центру; до 0:59 — в середине, дальше 0:30, 1:30…",
        "@topskin_tiktok · заявки @topskin_robot",
    ),
    Company(
        "wondercase", "WonderCase",
        Rule("wondercase", width=1.0, duck=0.15),
        {"wondercase_main": "WonderCase", "wondercase_red": "WonderCase — красный (контент по сайту)"},
        [("TikTok / Reels", "Баннер, CS2-контент", "$40 / 100 000", "от 2 000 просм."),
         ("TikTok / Reels", "Баннер, не CS2", "$10 / 100 000", ""),
         ("YouTube Shorts", "Любой баннерный", "−50% от ставки", ""),
         ("TikTok / Reels", "Игра на сайте", "$350 / 100 000", "от 1 000 просм.")],
        ["По центру, не меньше 25% экрана, звук чётко слышен",
         "До 59 сек. — 1 баннер, 60–119 — 2, 120–179 — 3", "Подать за 7 дней; ролик частями −40% к оплате"],
        "на всю ширину по центру; 1 баннер до минуты, дальше +1 за минуту",
        "@WonderCase_TikTok · менеджер @wonder_tt",
    ),
    Company(
        "natria", "NATRIA VPN",
        Rule("natria", width=1.0, duck=0.15),
        {"natria_vpn": "NATRIA VPN"},
        [("TikTok", "Баннер", "до $200 / 100 000", "зависит от категории"),
         ("YouTube Shorts", "Баннер", "до $144 / 100 000", ""),
         ("Все", "Конкурс креативов", "2 приза по $500", "итоги 31.10")],
        ["До 2 мин. — 1 раз в середине; от 2 мин. — на 0:30 и 1:30",
         "По центру, не меньше 25% экрана, хромакей удалить", "Старый баннер не принимают с 18.10"],
        "на всю ширину по центру с вырезанным хромакеем; до 2 мин. — в середине",
        "@natriavpn_media · заявки @NatriaVPN_ApplicationsBot",
    ),
    Company(
        "getrush", "GetRush",
        Rule("centered", width=1.0),
        {"getrush_3x1": "GetRush — Pizdyk"},
        [("Все", "Баннер в любом контенте", "$100 / 100 000", ""),
         ("Все", "Уникальный промо-контент", "$300 / 100 000", ""),
         ("Все", "Ролики, записанные на сайте", "$500 / 100 000", "от 1 000 просм.")],
        ["Баннер в середину видео, можно небольшую прозрачность", "Выплаты от $20"],
        "на всю ширину по центру, в середине ролика",
        "@getrush_tiktok · менеджер @getrush_manager",
    ),
]

# Красный баннер WonderCase по правилам ставится сверху или снизу, а не по центру
VARIANT_RULES = {"wondercase_red": Rule("wondercase", width=0.6, pos="video_bottom", duck=1.0)}

COMPANY_BY_KEY = {c.key: c for c in COMPANIES}
COMPANY_BY_BANNER = {bid: c for c in COMPANIES for bid in c.variants}

# Остальные рекламодатели из таблицы — баннера в архиве нет, только расценки
OTHER_RATES = [
    ("GGStore", "от 350 ₽", "баннер через бота @ggshorts_bot"),
    ("Plati market", "от 200 ₽", "баннер в личке @xzxzxxxxxss"),
    ("Dragon Money", "от 250 ₽", "баннер у @drgn_tt; казино — риск бана"),
    ("DarkStore", "от 250 ₽", "баннер через поддержку"),
    ("UPGRADER", "от 1000 ₽", "баннер по почте marketing@upgrader.pro"),
    ("Skinity", "от $50", "на паузе"),
    ("Skinhouse", "от $50", "skinhouse.partners/tiktok"),
    ("GGSEL", "до $10", "через бота PROMIQO"),
    ("RubUP", "≈ $300", "баннер в геймплей НЕ ставить"),
    ("БУБА VPN", "—", "закрыт, переехал в NATRIA"),
]


@dataclass
class Banner:
    id: str
    path: Path
    company: Company
    rule: Rule
    w: int = 0          # размер видимой части (кадр файла — в два раза выше из-за маски)
    h: int = 0
    duration: float = 0.0
    has_audio: bool = False
    _probed: bool = field(default=False, repr=False)

    def probe(self, ffmpeg: str) -> "Banner":
        if self._probed:
            return self
        err = subprocess.run([ffmpeg, "-hide_banner", "-i", str(self.path)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stderr
        w, h2 = map(int, re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err).groups())
        hh, mm, ss = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
        self.w, self.h = w, h2 // 2
        self.duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
        self.has_audio = "Audio:" in err
        self._probed = True
        return self

    def starts(self, d: float) -> list[float]:
        return TIMINGS[self.rule.timing](d, self.duration)

    @property
    def pauses(self) -> bool:
        """Пауза только у баннеров с озвучкой: беззвучный баннер идёт поверх видео."""
        return self.rule.pause and self.has_audio


def get_banner(bid: str) -> Banner | None:
    company = COMPANY_BY_BANNER.get(bid)
    if not company:
        return None
    return Banner(bid, BANNER_DIR / f"{bid}.mp4", company, VARIANT_RULES.get(bid, company.rule))


# --- Тексты для Telegram (HTML) -----------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def company_card(c: Company, banner: Banner | None = None) -> str:
    lines = [f"<b>{_esc(c.name)}</b>  ·  {_esc(c.contacts)}", "", "<b>Расценки (за 100 000 просмотров):</b>"]
    for platform, kind, rate, cond in c.rates:
        tail = f" — <i>{_esc(cond)}</i>" if cond else ""
        lines.append(f"• {_esc(platform)}, {_esc(kind)}: <b>{_esc(rate)}</b>{tail}")
    lines += ["", "<b>Условия:</b>"] + [f"• {_esc(t)}" for t in c.terms]
    lines += ["", f"🎯 <b>Бот поставит баннер:</b> {_esc(c.how)}"]
    if banner:
        lines.append("⏸ На время баннера основное видео встаёт на паузу — звучит только реклама"
                     if banner.pauses else "▶️ Баннер без озвучки — идёт поверх видео, без паузы")
    elif c.rule.pause:
        lines.append("⏸ Баннеры с озвучкой ставят видео на паузу, беззвучные идут поверх")
    return "\n".join(lines)


def rates_table() -> str:
    lines = ["<b>💰 Расценки рекламодателей</b> (за 100 000 просмотров, данные на 19.10.2026)", ""]
    for c in COMPANIES:
        best = " / ".join(dict.fromkeys(r[2] for r in c.rates[:3]))
        lines.append(f"✅ <b>{_esc(c.name)}</b>: {_esc(best)}")
    lines += ["", "<b>Без готового баннера</b> (нужно запросить):"]
    for name, rate, note in OTHER_RATES:
        lines.append(f"▫️ <b>{_esc(name)}</b>: {_esc(rate)} — <i>{_esc(note)}</i>")
    lines += ["", "<i>Ставки часто меняются — сверяйтесь с каналами рекламодателей.</i>"]
    return "\n".join(lines)
