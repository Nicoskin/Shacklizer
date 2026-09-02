"""Готовые пресеты — от «чуть подлагивает» до «связь оборвалась».

Главная ручка, отличающая лёгкие пресеты от тяжёлых, — `intra`: как часто
кусок кадра приходит целиком и чинится начисто. Чем она ниже, тем дольше
живёт однажды заехавший артефакт.
"""

from __future__ import annotations

from .core import Params
from .ffmpeg_engine import FFParams

PRESETS: dict[str, Params] = {
    # Всё почти нормально: просто дешёвая камера и узкий канал.
    "Лёгкий лаг": Params(
        width=416, frames=4, loss=0.05, burst=0.6, intra=0.30,
        mv=4.0, drift=1.2, smear=0.3, freeze=0.35, ghost=0.08,
        qp=0.18, cutoff=9, chroma_sub=1, chroma_loss=1.2, chroma_shift=1.0,
        exposure=0.08, bloom=0.12, noise=1.5,
    ),

    # Классика созвона: мыльно, местами куски съехали.
    "Плохая связь": Params(
        width=320, frames=8, loss=0.10, burst=0.45, intra=0.12,
        mv=7.0, drift=2.0, smear=0.45, freeze=0.20, ghost=0.15,
        qp=0.35, cutoff=6, chroma_sub=2, chroma_loss=1.6, chroma_shift=2.0,
        exposure=0.15, bloom=0.20, noise=2.0,
    ),

    # То, что зовут «шакал»: мыло, пересвет, лицо расползлось.
    "Шакал": Params(
        width=240, frames=10, loss=0.14, burst=0.35, intra=0.07,
        mv=8.0, drift=2.5, smear=0.5, freeze=0.2, ghost=0.2,
        qp=0.5, cutoff=4, chroma_sub=2, chroma_loss=1.8, chroma_shift=3.0,
        exposure=0.25, bloom=0.3, noise=2.5, jpeg=25,
    ),

    # Кадр посыпался: полосы тянутся из позапрошлого кадра.
    "Разрыв связи": Params(
        width=272, frames=14, loss=0.20, burst=0.22, intra=0.035,
        mv=12.0, drift=3.5, zoom=0.006, smear=0.55, freeze=0.15, ghost=0.25,
        qp=0.5, cutoff=4, chroma_sub=2, chroma_loss=2.0, chroma_shift=4.0,
        exposure=0.28, bloom=0.35, noise=3.0, jpeg=20,
    ),

    # Чистый датамош: длинные всплески потерь, всё уезжает в одну сторону.
    "Датамош": Params(
        width=288, frames=18, loss=0.28, burst=0.12, intra=0.015, slice_len=20,
        mv=16.0, drift=5.0, zoom=0.010, smear=0.65, freeze=0.1, ghost=0.3,
        qp=0.42, cutoff=4, chroma_sub=2, chroma_loss=2.2, chroma_shift=5.0,
        exposure=0.2, bloom=0.3, noise=2.0, jpeg=22,
    ),
}

# То же самое для движка настоящего H.264. Ручки там другие, поэтому
# набор отдельный; главная — refresh: период intra-обновления. Чем он
# длиннее, тем дольше живёт однажды заехавший артефакт.
FF_PRESETS: dict[str, FFParams] = {
    "Лёгкий лаг": FFParams(width=448, bitrate=150, slices=6, frames=28, refresh=10,
                           motion=16, loss=0.08, burst=0.55, corrupt=0.35),
    "Плохая связь": FFParams(width=352, bitrate=90, slices=8, frames=36, refresh=12,
                             motion=26, loss=0.10, burst=0.45, corrupt=0.5),
    "Шакал": FFParams(width=256, bitrate=42, slices=8, frames=40, refresh=18,
                      motion=30, loss=0.15, burst=0.35, corrupt=0.5),
    "Разрыв связи": FFParams(width=288, bitrate=55, slices=10, frames=44, refresh=28,
                             motion=40, loss=0.20, burst=0.25, corrupt=0.5),
    "Датамош": FFParams(width=320, bitrate=70, slices=4, frames=48, refresh=20,
                        motion=58, loss=0.14, burst=0.35, corrupt=0.4),
}

DEFAULT = "Плохая связь"

# Короткие имена для командной строки — русские в консоли набирать больно.
ALIASES: dict[str, str] = {
    "light": "Лёгкий лаг",
    "bad": "Плохая связь",
    "jackal": "Шакал",
    "broken": "Разрыв связи",
    "datamosh": "Датамош",
}


def canonical(name: str) -> str:
    """Русское имя пресета по алиасу или по самому имени, регистр не важен."""
    if name in PRESETS:
        return name
    key = ALIASES.get(name.lower())
    if key:
        return key
    for full in PRESETS:
        if full.lower() == name.lower():
            return full
    known = ", ".join(ALIASES)
    raise KeyError(f"unknown preset {name!r}; try one of: {known}")


def resolve(name: str) -> Params:
    """Настройки движка-симуляции."""
    return PRESETS[canonical(name)]


def resolve_ff(name: str) -> FFParams:
    """Настройки движка настоящего H.264."""
    return FF_PRESETS[canonical(name)]
