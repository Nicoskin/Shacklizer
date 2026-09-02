"""Ядро «шакалайзера».

Не просто ухудшение качества, а имитация приёма видеопотока, в котором
теряются UDP-пакеты.

Главная мысль, из-за которой картинка выглядит именно «поплывшей», а не
просто мыльной: в H.264 кадр почти целиком собирается из предыдущего
кадра по векторам движения. Поэтому ошибка, один раз попавшая в кадр,
не исчезает со следующим пакетом — она едет дальше вместе с движением и
живёт до ближайшего intra-обновления этого куска. Мы моделируем ровно
это, храня отдельно:

    base — то, что передатчик реально хочет показать (пожатый оригинал);
    E    — накопленная ошибка декодера.

Показывается всегда `base + E`. Дальше по кадрам:

    E  едет по вектору движения          (ошибка распространяется)
    E  обнуляется на intra-блоках        (кусок «чинится»)
    E  переписывается на потерянных      (декодер выкручивается как умеет:
       блоках                             тянет блок из старого кадра по
                                          протухшему вектору, размазывает
                                          последнюю удачную строку вниз
                                          или просто морозит картинку)

Пакеты теряются не поодиночке, а всплесками — модель Гилберта-Эллиотта,
как в реальной сети. Один пакет = один слайс = полоса макроблоков.
"""

from __future__ import annotations

import dataclasses
import io
import math

import numpy as np
from PIL import Image, ImageFilter, ImageOps

MB = 16  # размер макроблока по яркости


# --------------------------------------------------------------------------
# Параметры
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Params:
    """Все ручки, которые крутит пользователь."""

    # --- канал ---
    width: int = 320          # рабочее разрешение потока, px по ширине
    frames: int = 8           # сколько кадров «болтанки» проиграть
    loss: float = 0.10        # вероятность потерять пакет (p в модели Г-Э)
    burst: float = 0.45       # вероятность восстановиться (r); 1/r ≈ длина всплеска
    slice_len: int = 0        # макроблоков в пакете; 0 = половина строки MB

    # --- поведение кодека/декодера ---
    intra: float = 0.10       # доля блоков, обновляемых начисто за кадр
    mv: float = 7.0           # разброс векторов движения при concealment, px
    drift: float = 2.0        # дрейф кадра за кадр, px (по нему едет ошибка)
    zoom: float = 0.004       # наплыв за кадр (доля)
    smear: float = 0.45       # доля потерь, размазанных вертикально
    freeze: float = 0.20      # доля потерь, просто замороженных
    ghost: float = 0.15       # подмешивание опорного кадра целиком

    # --- кодек ---
    qp: float = 0.35          # жёсткость квантования DCT, 0..1
    cutoff: int = 6           # обрезка высоких частот, 0..14 (меньше = мыльнее)
    chroma_sub: int = 2       # огрубление цвета: 1 = 4:2:0, 2 = /4, 3 = /8
    chroma_loss: float = 1.6  # во сколько раз цвет сыпется чаще яркости
    chroma_shift: float = 2.0  # разъезд Cb/Cr, px

    # --- камера/вывод ---
    exposure: float = 0.15    # пересвет дешёвой фронталки
    bloom: float = 0.20       # свечение в светах
    noise: float = 2.0        # шум сенсора, ед. яркости
    jpeg: int = 0             # финальный JPEG quality, 0 = выключено
    out_width: int = 0        # 0 = вернуть исходный размер
    seed: int | None = None   # None = каждый раз новый результат


def chroma_ratio(p: Params) -> int:
    """Во сколько раз цветовые плоскости мельче яркостной.

    Степень двойки, делящая 16: макроблок должен делиться нацело, а
    цветовая плоскость — оставаться кратной блоку DCT 8x8.
    """
    return 2 ** max(1, min(3, int(p.chroma_sub)))


# --------------------------------------------------------------------------
# Цветовое пространство
# --------------------------------------------------------------------------

_RGB2YCC = np.array([
    [0.299, 0.587, 0.114],
    [-0.168736, -0.331264, 0.5],
    [0.5, -0.418688, -0.081312],
], dtype=np.float32)

_YCC2RGB = np.array([
    [1.0, 0.0, 1.402],
    [1.0, -0.344136, -0.714136],
    [1.0, 1.772, 0.0],
], dtype=np.float32)


def rgb_to_ycc(rgb: np.ndarray) -> np.ndarray:
    out = rgb @ _RGB2YCC.T
    out[..., 1:] += 128.0
    return out


def ycc_to_rgb(ycc: np.ndarray) -> np.ndarray:
    tmp = ycc.copy()
    tmp[..., 1:] -= 128.0
    return tmp @ _YCC2RGB.T


# --------------------------------------------------------------------------
# DCT 8x8
# --------------------------------------------------------------------------

def _dct_matrix(n: int = 8) -> np.ndarray:
    k = np.arange(n, dtype=np.float32)[:, None]
    x = np.arange(n, dtype=np.float32)[None, :]
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n)) * math.sqrt(2.0 / n)
    m[0] /= math.sqrt(2.0)
    return m.astype(np.float32)


_D = _dct_matrix()

# Стандартная таблица квантования JPEG (luma) — база, которую мы масштабируем.
_QTABLE = np.array([
    [16, 11, 10, 16, 24, 40, 51, 61],
    [12, 12, 14, 19, 26, 58, 60, 55],
    [14, 13, 16, 24, 40, 57, 69, 56],
    [14, 17, 22, 29, 51, 87, 80, 62],
    [18, 22, 37, 56, 68, 109, 103, 77],
    [24, 35, 55, 64, 81, 104, 113, 92],
    [49, 64, 78, 87, 103, 121, 120, 101],
    [72, 92, 95, 98, 112, 100, 103, 99],
], dtype=np.float32)

_FREQ = np.add.outer(np.arange(8), np.arange(8))


def _blocks(plane: np.ndarray) -> np.ndarray:
    h, w = plane.shape
    return plane.reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3)


def _unblocks(b: np.ndarray) -> np.ndarray:
    bh, bw = b.shape[:2]
    return b.transpose(0, 2, 1, 3).reshape(bh * 8, bw * 8)


def quantize(plane: np.ndarray, qp: float, cutoff: int,
             dc_max: float = 24.0) -> np.ndarray:
    """Грубое DCT-квантование: блочность плюс звон на границах.

    Шаг по DC ограничен `dc_max`: в реальном кодеке средний уровень блока
    передаётся точно, а битов лишается детализация. Без этого ограничения
    на высоких qp блок теряет собственную яркость и цвет — картинка
    распадается на несколько кислотных уровней вместо мыла.
    """
    if qp <= 0 and cutoff >= 14:
        return plane
    q = _QTABLE * (1.0 + qp * 42.0)
    q[0, 0] = min(q[0, 0], dc_max)
    mask = (_FREQ <= cutoff).astype(np.float32)

    coeff = _D @ _blocks(plane - 128.0) @ _D.T
    coeff = np.round(coeff / q) * q * mask
    return _unblocks(_D.T @ coeff @ _D) + 128.0


# --------------------------------------------------------------------------
# Сеть
# --------------------------------------------------------------------------

def gilbert_elliott(n: int, p: float, r: float, rng: np.random.Generator) -> np.ndarray:
    """Потери всплесками: p — уйти в плохое состояние, r — выйти из него."""
    out = np.zeros(n, dtype=bool)
    bad = False
    rolls = rng.random(n)
    for i in range(n):
        bad = (rolls[i] > r) if bad else (rolls[i] < p)
        out[i] = bad
    return out


def _packet_loss(total_mb: int, slice_len: int, p: float, r: float,
                 rng: np.random.Generator) -> np.ndarray:
    """Маска потерянных макроблоков: теряем целыми пакетами-слайсами."""
    n_packets = math.ceil(total_mb / slice_len)
    bad = gilbert_elliott(n_packets, p, max(1e-3, r), rng)
    return np.repeat(bad, slice_len)[:total_mb]


# --------------------------------------------------------------------------
# Декодер
# --------------------------------------------------------------------------

def _sample(ref: np.ndarray, y0: int, y1: int, x0: int, x1: int,
            dy: int, dx: int) -> np.ndarray:
    """Достаём блок из опорного кадра со смещением, края залипают."""
    h, w = ref.shape
    rows = np.clip(np.arange(y0, y1) + dy, 0, h - 1)
    cols = np.clip(np.arange(x0, x1) + dx, 0, w - 1)
    return ref[np.ix_(rows, cols)]


def _warp(plane: np.ndarray, dx: float, dy: float, zoom: float) -> np.ndarray:
    """Кадр уезжает: сдвиг плюс лёгкий наплыв. По этому же пути едет ошибка."""
    h, w = plane.shape
    yy = (np.arange(h, dtype=np.float32) - h / 2) * (1.0 - zoom) + h / 2 - dy
    xx = (np.arange(w, dtype=np.float32) - w / 2) * (1.0 - zoom) + w / 2 - dx
    yi = np.clip(np.round(yy).astype(np.int32), 0, h - 1)
    xi = np.clip(np.round(xx).astype(np.int32), 0, w - 1)
    return plane[np.ix_(yi, xi)]


class _Planes:
    """Тройка Y/Cb/Cr, где цвет живёт в `sub` раз мельче яркости."""

    __slots__ = ("y", "cb", "cr", "sub")

    def __init__(self, y: np.ndarray, cb: np.ndarray, cr: np.ndarray, sub: int):
        self.y, self.cb, self.cr, self.sub = y, cb, cr, sub

    def copy(self) -> "_Planes":
        return _Planes(self.y.copy(), self.cb.copy(), self.cr.copy(), self.sub)

    def clip(self) -> "_Planes":
        """Декодер работает в 8 битах: за диапазон уехать нечему.

        Без этого ошибка копится без ограничений и цвет уходит в неон,
        чего на реальном созвоне не бывает.
        """
        return _Planes(np.clip(self.y, 0.0, 255.0), np.clip(self.cb, 0.0, 255.0),
                       np.clip(self.cr, 0.0, 255.0), self.sub)

    def warp(self, dx: float, dy: float, zoom: float) -> "_Planes":
        s = self.sub
        return _Planes(_warp(self.y, dx, dy, zoom),
                       _warp(self.cb, dx / s, dy / s, zoom),
                       _warp(self.cr, dx / s, dy / s, zoom), s)

    def __add__(self, other: "_Planes") -> "_Planes":
        return _Planes(self.y + other.y, self.cb + other.cb,
                       self.cr + other.cr, self.sub)

    def __sub__(self, other: "_Planes") -> "_Planes":
        return _Planes(self.y - other.y, self.cb - other.cb,
                       self.cr - other.cr, self.sub)


def _mb_bounds(mb: int, mb_w: int, sub: int):
    my, mx = divmod(int(mb), mb_w)
    y0, x0 = my * MB, mx * MB
    return (y0, y0 + MB, x0, x0 + MB,
            y0 // sub, (y0 + MB) // sub, x0 // sub, (x0 + MB) // sub)


def _conceal(cur: _Planes, prev: _Planes, lost: np.ndarray, mb_w: int,
             p: Params, rng: np.random.Generator) -> None:
    """Достроить потерянные макроблоки. In-place и в растровом порядке —
    поэтому вертикальный размаз протягивается через несколько блоков подряд."""
    idx = np.flatnonzero(lost)
    if idx.size == 0:
        return

    sub = cur.sub
    modes = rng.random(idx.size)
    jitter = rng.normal(0.0, p.mv, size=(idx.size, 2))

    for k, mb in enumerate(idx):
        y0, y1, x0, x1, cy0, cy1, cx0, cx1 = _mb_bounds(mb, mb_w, sub)
        mode = modes[k]

        if mode < p.smear and y0 > 0:
            # Рисовать нечего — тянем последнюю удачную строку вниз.
            cur.y[y0:y1, x0:x1] = cur.y[y0 - 1:y0, x0:x1]
            if cy0 > 0:
                cur.cb[cy0:cy1, cx0:cx1] = cur.cb[cy0 - 1:cy0, cx0:cx1]
                cur.cr[cy0:cy1, cx0:cx1] = cur.cr[cy0 - 1:cy0, cx0:cx1]
            continue

        if mode < p.smear + p.freeze:
            dy = dx = 0  # freeze: оставляем то, что было
        else:
            dy, dx = int(jitter[k, 0]), int(jitter[k, 1])

        cur.y[y0:y1, x0:x1] = _sample(prev.y, y0, y1, x0, x1, dy, dx)
        cdy, cdx = dy // sub, dx // sub
        cur.cb[cy0:cy1, cx0:cx1] = _sample(prev.cb, cy0, cy1, cx0, cx1, cdy, cdx)
        cur.cr[cy0:cy1, cx0:cx1] = _sample(prev.cr, cy0, cy1, cx0, cx1, cdy, cdx)


def _chroma_only_loss(cur: _Planes, prev: _Planes, lost: np.ndarray,
                      mb_w: int, p: Params, rng: np.random.Generator) -> None:
    """Цвет едет в своих пакетах и сыпется отдельно от яркости."""
    sub = cur.sub
    for mb in np.flatnonzero(lost):
        _, _, _, _, cy0, cy1, cx0, cx1 = _mb_bounds(mb, mb_w, sub)
        d = int(rng.normal(0.0, p.mv)) // sub
        cur.cb[cy0:cy1, cx0:cx1] = _sample(prev.cb, cy0, cy1, cx0, cx1, d, d)
        cur.cr[cy0:cy1, cx0:cx1] = _sample(prev.cr, cy0, cy1, cx0, cx1, -d, d)


def _reset_intra(err: _Planes, mask: np.ndarray, mb_w: int) -> None:
    """Intra-обновление: кусок кадра приходит целиком и чинится начисто."""
    sub = err.sub
    for mb in np.flatnonzero(mask):
        y0, y1, x0, x1, cy0, cy1, cx0, cx1 = _mb_bounds(mb, mb_w, sub)
        err.y[y0:y1, x0:x1] = 0.0
        err.cb[cy0:cy1, cx0:cx1] = 0.0
        err.cr[cy0:cy1, cx0:cx1] = 0.0


# --------------------------------------------------------------------------
# Сборка
# --------------------------------------------------------------------------

def _shift(plane: np.ndarray, dx: int) -> np.ndarray:
    return np.roll(plane, dx, axis=1) if dx else plane


def _blur(plane: np.ndarray, radius: float) -> np.ndarray:
    if radius <= 0:
        return plane
    img = Image.fromarray(np.clip(plane, 0, 255).astype(np.uint8), "L")
    return np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


def _encode(image: Image.Image, p: Params, w: int, h: int, sub: int) -> _Planes:
    """Что передатчик хочет показать: даунскейл, пересвет, 4:2:0, квантование."""
    rgb = np.asarray(image.resize((w, h), Image.LANCZOS), dtype=np.float32)

    if p.exposure:
        x = np.clip(rgb / 255.0, 0.0, 1.0)
        rgb = 255.0 * (1.0 - (1.0 - x) ** (1.0 + p.exposure * 1.6))

    ycc = rgb_to_ycc(rgb)
    y = np.ascontiguousarray(ycc[..., 0])
    ch, cw = h // sub, w // sub
    cb = np.array(Image.fromarray(ycc[..., 1]).resize((cw, ch), Image.BOX), dtype=np.float32)
    cr = np.array(Image.fromarray(ycc[..., 2]).resize((cw, ch), Image.BOX), dtype=np.float32)

    # Цвет и так прорежен в sub раз, поэтому давим его сильнее по деталям,
    # но мягче по DC — иначе кожа уезжает в зелень.
    return _Planes(quantize(y, p.qp, p.cutoff, dc_max=24.0),
                   quantize(cb, p.qp * 1.3, max(0, p.cutoff - 2), dc_max=14.0),
                   quantize(cr, p.qp * 1.3, max(0, p.cutoff - 2), dc_max=14.0),
                   sub).clip()


def degrade(image: Image.Image, p: Params) -> Image.Image:
    """Прогнать картинку через «плохой канал» и вернуть, что доехало."""
    rng = np.random.default_rng(p.seed)

    src = ImageOps.exif_transpose(image).convert("RGB")
    ow, oh = src.size

    # Сетка: 16 по яркости и 8 по цвету после прореживания в sub раз.
    sub = chroma_ratio(p)
    align = max(MB, 8 * sub)
    w = max(align, int(round(p.width / align)) * align)
    h = max(align, int(round(w * oh / ow / align)) * align)

    base = _encode(src, p, w, h, sub)

    mb_w = w // MB
    total_mb = mb_w * (h // MB)
    slice_len = p.slice_len if p.slice_len > 0 else max(1, mb_w // 2)

    zero = np.zeros_like
    err = _Planes(zero(base.y), zero(base.cb), zero(base.cr), sub)
    dx_acc = dy_acc = 0.0

    for _ in range(max(1, p.frames)):
        # 1. кадр сместился — накопленная ошибка едет вместе с ним
        dx_acc += rng.normal(0.0, p.drift)
        dy_acc += rng.normal(0.0, p.drift)
        err = err.warp(dx_acc, dy_acc, p.zoom)

        # 2. intra-обновление чинит часть блоков начисто
        if p.intra > 0:
            _reset_intra(err, rng.random(total_mb) < p.intra, mb_w)

        # 3. то, что декодер показал бы, если бы всё доехало
        prev = base + err
        cur = prev.copy()

        # 4. ...но часть пакетов не доехала
        lost = _packet_loss(total_mb, slice_len, p.loss, p.burst, rng)
        _conceal(cur, prev, lost, mb_w, p, rng)

        if p.chroma_loss > 1.0:
            extra = _packet_loss(total_mb, slice_len,
                                 min(0.95, p.loss * p.chroma_loss), p.burst, rng)
            _chroma_only_loss(cur, prev, extra & ~lost, mb_w, p, rng)

        # 5. призрак прошлого кадра поверх — межкадровое смешение
        if p.ghost > 0:
            g = p.ghost
            cur = _Planes(cur.y * (1 - g) + prev.y * g,
                          cur.cb * (1 - g) + prev.cb * g,
                          cur.cr * (1 - g) + prev.cr * g, sub)

        err = cur.clip() - base

    out_planes = (base + err).clip()
    y, cb, cr = out_planes.y, out_planes.cb, out_planes.cr

    # --- обратно в RGB ---
    if p.chroma_shift:
        s = int(round(p.chroma_shift))
        cb, cr = _shift(cb, s), _shift(cr, -s)
    cb, cr = _blur(cb, 0.8), _blur(cr, 0.8)

    cb_up = np.asarray(Image.fromarray(cb).resize((w, h), Image.BILINEAR), dtype=np.float32)
    cr_up = np.asarray(Image.fromarray(cr).resize((w, h), Image.BILINEAR), dtype=np.float32)
    out = ycc_to_rgb(np.dstack([y, cb_up, cr_up]))

    if p.bloom > 0:
        hot = np.clip(out - 200.0, 0, 55)
        hot_img = Image.fromarray(hot.astype(np.uint8), "RGB")
        hot_b = np.asarray(hot_img.filter(ImageFilter.GaussianBlur(5.0)), dtype=np.float32)
        out = out + hot_b * p.bloom

    if p.noise > 0:
        out = out + rng.normal(0.0, p.noise, size=out.shape).astype(np.float32)

    res = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")

    # апскейл билинейкой — то самое мыло растянутого потока
    target_w = p.out_width if p.out_width > 0 else ow
    target_h = max(1, int(round(target_w * oh / ow)))
    res = res.resize((target_w, target_h), Image.BILINEAR)

    if p.jpeg:
        buf = io.BytesIO()
        res.save(buf, "JPEG", quality=int(p.jpeg), subsampling=2)
        buf.seek(0)
        res = Image.open(buf).convert("RGB")

    return res
