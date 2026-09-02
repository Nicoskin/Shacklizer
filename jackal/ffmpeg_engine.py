"""Второй движок: настоящий H.264, у которого мы физически выкидываем пакеты.

Отличие от `core`: тут ничего не имитируется. Картинка реально кодируется
x264, поток режется на NAL-юниты (один NAL = одна RTP-посылка = один UDP-
пакет), часть юнитов выбрасывается или бьётся, и это скармливается
настоящему декодеру с включённым error concealment. Артефакты получаются
ровно те, что бывают в жизни, — со всеми причудами конкретного декодера.

Нужен ffmpeg в PATH.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

START_CODE = b"\x00\x00\x01"


@dataclasses.dataclass
class FFParams:
    width: int = 320        # разрешение кодирования
    frames: int = 40        # длина клипа в кадрах (ошибка копится по ходу)
    bitrate: int = 60       # kbit/s — чем меньше, тем крупнее блоки
    slices: int = 8         # слайсов на кадр = сколько пакетов на кадр
    refresh: int = 12       # период intra-обновления, кадров (больше = дольше живёт артефакт)
    loss: float = 0.10      # вероятность потерять пакет
    burst: float = 0.5      # вероятность восстановиться после потери
    corrupt: float = 0.5    # доля «потерь», которые не выброшены, а побиты
    motion: float = 26.0    # амплитуда движения камеры, px
    grab: int = -1          # какой кадр забрать (-1 = последний удачный)
    heal: bool = True       # затягивать зелёные дыры содержимым прошлых кадров
    out_width: int = 0
    seed: int | None = None


def available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def split_nals(data: bytes) -> list[bytes]:
    """Разбить Annex-B поток на NAL-юниты вместе со стартовыми кодами."""
    positions = []
    i = data.find(START_CODE)
    while i != -1:
        positions.append(i)
        i = data.find(START_CODE, i + 3)
    if not positions:
        return [data]
    nals = []
    for a, b in zip(positions, positions[1:] + [len(data)]):
        start = a - 1 if a > 0 and data[a - 1] == 0 else a
        nals.append(data[start:b])
    return nals


def _nal_type(nal: bytes) -> int:
    i = nal.find(START_CODE)
    return nal[i + 3] & 0x1F if i != -1 and len(nal) > i + 3 else 0


def drop_packets(data: bytes, p: FFParams) -> bytes:
    """Выкинуть/побить часть NAL-юнитов. Заголовки и первый IDR не трогаем —
    иначе декодеру не от чего отталкиваться и мы получим просто пустоту."""
    rng = np.random.default_rng(p.seed)
    nals = split_nals(data)

    out: list[bytes] = []
    seen_idr = False
    bad = False

    for nal in nals:
        t = _nal_type(nal)
        # 7=SPS, 8=PPS, 5=IDR, 9=AUD, 6=SEI
        if t in (7, 8, 9, 6) or (t == 5 and not seen_idr):
            if t == 5:
                seen_idr = True
            out.append(nal)
            continue

        bad = (rng.random() > p.burst) if bad else (rng.random() < p.loss)
        if not bad:
            out.append(nal)
            continue

        if rng.random() < p.corrupt and len(nal) > 16:
            # пакет доехал битым: CRC нет, декодер честно рисует мусор
            buf = bytearray(nal)
            n = max(1, len(buf) // 40)
            idx = rng.integers(8, len(buf), size=n)
            for j in idx:
                buf[int(j)] ^= int(rng.integers(1, 256))
            out.append(bytes(buf))
        # иначе пакет просто потерян и не попадает в поток

    return b"".join(out)


def _unset_mask(rgb: np.ndarray) -> np.ndarray:
    """Ядовито-зелёные пятна — это макроблоки, до которых декодер вообще не
    дошёл, и в них осталась неинициализированная YUV-нулёвка."""
    green = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
    return (green > 60) & (rgb[..., 0] < 110) & (rgb[..., 2] < 110)


def _heal(frames: list[Path], index: int) -> Image.Image:
    """Заткнуть дыры тем, что было в этом месте раньше.

    Нормальный плеер показывает последнюю удачную картинку, а не зелень,
    так что это ближе к жизни, чем сырой вывод ffmpeg.
    """
    def read(path: Path) -> np.ndarray:
        # Image.open держит файл открытым лениво, а на Windows это мешает
        # потом удалить временную папку — поэтому читаем через with.
        with Image.open(path) as im:
            return np.asarray(im.convert("RGB"))

    arr = read(frames[index]).astype(np.uint8)
    mask = _unset_mask(arr.astype(np.int16))
    if not mask.any():
        return Image.fromarray(arr)

    arr = arr.copy()
    for i in range(index - 1, -1, -1):
        if not mask.any():
            break
        prev = read(frames[i])
        if prev.shape != arr.shape:
            break
        take = mask & ~_unset_mask(prev.astype(np.int16))
        arr[take] = prev[take]
        mask &= ~take
    return Image.fromarray(arr)


def degrade(image: Image.Image, p: FFParams) -> Image.Image:
    if not available():
        raise RuntimeError("ffmpeg not found in PATH")

    src = ImageOps.exif_transpose(image).convert("RGB")
    ow, oh = src.size

    w = max(64, (p.width // 2) * 2)
    h = max(64, (int(round(w * oh / ow)) // 2) * 2)

    # ignore_cleanup_errors: на Windows файл может ещё секунду держать
    # антивирус или не успевший закрыться ffmpeg — это не повод падать.
    with tempfile.TemporaryDirectory(prefix="jackal_", ignore_cleanup_errors=True) as tmp:
        td = Path(tmp)
        png = td / "src.png"
        src.save(png)

        # 1. кодируем: лёгкое дрожание камеры даёт настоящие векторы движения
        m = p.motion
        big_w, big_h = w + int(2 * m) + 4, h + int(2 * m) + 4
        big_w -= big_w % 2
        big_h -= big_h % 2
        vf = (
            f"scale={big_w}:{big_h},"
            f"crop={w}:{h}:"
            f"'(in_w-out_w)/2+{m}*sin(n/7)':"
            f"'(in_h-out_h)/2+{m}*cos(n/9)'"
        )
        raw = td / "raw.h264"
        enc = _run([
            "ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", str(png),
            "-frames:v", str(max(2, p.frames)), "-r", "15", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", f"{p.bitrate}k", "-maxrate", f"{p.bitrate}k",
            "-bufsize", f"{p.bitrate * 2}k", "-bf", "0",
            "-g", str(max(2, p.refresh)),
            # один поток — иначе x264 выдаёт разный битстрим от запуска к
            # запуску и seed перестаёт что-либо воспроизводить
            "-threads", "1",
            # intra-refresh — то, чем реальные видеозвонки лечатся от потерь:
            # по кадру ползёт полоса intra-блоков и постепенно чинит картинку.
            # Без неё за 40 кадров любая потеря съедает кадр целиком.
            "-x264-params",
            f"slices={max(1, p.slices)}:scenecut=0:"
            f"intra-refresh=1:keyint={max(2, p.refresh)}:"
            f"threads=1:sliced-threads=0",
            "-f", "h264", str(raw),
        ])
        if not raw.exists() or raw.stat().st_size == 0:
            raise RuntimeError(f"encode failed: {enc.stderr.decode(errors='replace')[:400]}")

        # 2. теряем пакеты
        bad = td / "bad.h264"
        bad.write_bytes(drop_packets(raw.read_bytes(), p))

        # 3. декодируем как есть — пусть выкручивается
        outdir = td / "out"
        outdir.mkdir()
        _run([
            "ffmpeg", "-y", "-v", "error", "-err_detect", "ignore_err",
            "-ec", "guess_mvs+deblock+favor_inter", "-flags2", "+showall",
            "-fflags", "+genpts", "-i", str(bad),
            "-fps_mode", "passthrough", str(outdir / "f%04d.png"),
        ])

        got = sorted(outdir.glob("f*.png"))
        if not got:
            raise RuntimeError("decoder produced no frames (try lower loss)")
        idx = p.grab if -len(got) <= p.grab < len(got) else -1
        idx %= len(got)
        if p.heal:
            res = _heal(got, idx)
        else:
            with Image.open(got[idx]) as im:
                res = im.convert("RGB")

    target_w = p.out_width if p.out_width > 0 else ow
    target_h = max(1, int(round(target_w * oh / ow)))
    return res.resize((target_w, target_h), Image.BILINEAR)
