/* Пресеты и описания ручек для обоих движков.
   Один пресет несёт два набора настроек — они не пересекаются, потому что
   движки устроены принципиально по-разному. */

const SPECS = {
  h264: [
    { key: 'width', label: 'Разрешение потока', min: 96, max: 448, step: 16, unit: 'px' },
    { key: 'bitrate', label: 'Битрейт', min: 8, max: 400, step: 2, unit: 'kbit/s' },
    { key: 'slices', label: 'Слайсов в кадре', min: 1, max: 16, step: 1 },
    { key: 'frames', label: 'Длина связи', min: 8, max: 48, step: 1, unit: 'кадров' },
    { key: 'refresh', label: 'Период intra-обновления', min: 4, max: 64, step: 1, unit: 'кадров' },
    { key: 'motion', label: 'Движение камеры', min: 0, max: 70, step: 1, unit: 'px' },
    { key: 'loss', label: 'Потеря пакетов', min: 0, max: 0.6, step: 0.01, pct: true },
    { key: 'burst', label: 'Восстановление', min: 0.05, max: 1, step: 0.01, pct: true },
    { key: 'corrupt', label: 'Битые вместо потерянных', min: 0, max: 1, step: 0.05, pct: true },
    { key: 'grab', label: 'Кадр от конца', min: 0, max: 11, step: 1 },
  ],
  sim: [
    { key: 'width', label: 'Разрешение потока', min: 96, max: 640, step: 16, unit: 'px' },
    { key: 'frames', label: 'Кадров болтанки', min: 1, max: 40, step: 1 },
    { key: 'loss', label: 'Потеря пакетов', min: 0, max: 0.6, step: 0.01, pct: true },
    { key: 'burst', label: 'Восстановление', min: 0.05, max: 1, step: 0.01, pct: true },
    { key: 'intra', label: 'Intra-обновление', min: 0, max: 0.6, step: 0.005, pct: true },
    { key: 'mv', label: 'Разброс векторов', min: 0, max: 40, step: 0.5, unit: 'px' },
    { key: 'drift', label: 'Дрейф кадра', min: 0, max: 12, step: 0.1, unit: 'px' },
    { key: 'smear', label: 'Вертикальный размаз', min: 0, max: 1, step: 0.01, pct: true },
    { key: 'freeze', label: 'Заморозка блока', min: 0, max: 1, step: 0.01, pct: true },
    { key: 'ghost', label: 'Призрак прошлого кадра', min: 0, max: 0.6, step: 0.01, pct: true },
    { key: 'qp', label: 'Квантование', min: 0, max: 1, step: 0.01, pct: true },
    { key: 'cutoff', label: 'Срез высоких частот', min: 0, max: 14, step: 1 },
    { key: 'chromaSub', label: 'Огрубление цвета', min: 1, max: 3, step: 1 },
    { key: 'chromaShift', label: 'Разъезд цвета', min: 0, max: 10, step: 0.5, unit: 'px' },
    { key: 'exposure', label: 'Пересвет', min: 0, max: 0.8, step: 0.01, pct: true },
    { key: 'bloom', label: 'Свечение', min: 0, max: 1, step: 0.01, pct: true },
    { key: 'noise', label: 'Шум', min: 0, max: 10, step: 0.1 },
  ],
};

const PRESETS = {
  'Лёгкий лаг': {
    h264: { width: 448, bitrate: 150, slices: 6, frames: 28, refresh: 10, motion: 16, loss: 0.08, burst: 0.55, corrupt: 0.35, grab: 0 },
    sim: { width: 416, frames: 4, loss: 0.05, burst: 0.6, intra: 0.30, mv: 4, drift: 1.2, zoom: 0.004, smear: 0.3, freeze: 0.35, ghost: 0.08, qp: 0.18, cutoff: 9, chromaSub: 1, chromaLoss: 1.2, chromaShift: 1, exposure: 0.08, bloom: 0.12, noise: 1.5 },
  },
  'Плохая связь': {
    h264: { width: 352, bitrate: 90, slices: 8, frames: 36, refresh: 12, motion: 26, loss: 0.10, burst: 0.45, corrupt: 0.5, grab: 0 },
    sim: { width: 320, frames: 8, loss: 0.10, burst: 0.45, intra: 0.12, mv: 7, drift: 2, zoom: 0.004, smear: 0.45, freeze: 0.2, ghost: 0.15, qp: 0.35, cutoff: 6, chromaSub: 2, chromaLoss: 1.6, chromaShift: 2, exposure: 0.15, bloom: 0.2, noise: 2 },
  },
  'Шакал': {
    h264: { width: 256, bitrate: 42, slices: 8, frames: 40, refresh: 18, motion: 30, loss: 0.15, burst: 0.35, corrupt: 0.5, grab: 0 },
    sim: { width: 240, frames: 10, loss: 0.14, burst: 0.35, intra: 0.07, mv: 8, drift: 2.5, zoom: 0.004, smear: 0.5, freeze: 0.2, ghost: 0.2, qp: 0.5, cutoff: 4, chromaSub: 2, chromaLoss: 1.8, chromaShift: 3, exposure: 0.25, bloom: 0.3, noise: 2.5 },
  },
  'Разрыв связи': {
    h264: { width: 288, bitrate: 55, slices: 10, frames: 44, refresh: 28, motion: 40, loss: 0.20, burst: 0.25, corrupt: 0.5, grab: 0 },
    sim: { width: 272, frames: 14, loss: 0.20, burst: 0.22, intra: 0.035, mv: 12, drift: 3.5, zoom: 0.006, smear: 0.55, freeze: 0.15, ghost: 0.25, qp: 0.5, cutoff: 4, chromaSub: 2, chromaLoss: 2, chromaShift: 4, exposure: 0.28, bloom: 0.35, noise: 3 },
  },
  'Датамош': {
    h264: { width: 320, bitrate: 70, slices: 4, frames: 48, refresh: 20, motion: 58, loss: 0.14, burst: 0.35, corrupt: 0.4, grab: 0 },
    sim: { width: 288, frames: 18, loss: 0.28, burst: 0.12, intra: 0.015, mv: 16, drift: 5, zoom: 0.010, smear: 0.65, freeze: 0.1, ghost: 0.3, qp: 0.42, cutoff: 4, chromaSub: 2, chromaLoss: 2.2, chromaShift: 5, exposure: 0.2, bloom: 0.3, noise: 2 },
  },
};

const DEFAULT_PRESET = 'Плохая связь';
