/* Движок «Симуляция» — порт jackal/core.py на типизированные массивы.

   Считает не сам кодек, а его поведение при потерях. Отдельно хранится
   `base` (то, что передатчик хочет показать) и `E` (накопленная ошибка
   декодера); на экране всегда base + E. По кадрам ошибка едет по вектору
   движения, обнуляется на intra-блоках и переписывается на потерянных.

   Быстро (десятки миллисекунд) и не требует ничего скачивать. */

const Sim = (() => {

  const MB = 16;

  // --- генератор случайных чисел с seed, чтобы «Новый бросок» был воспроизводим
  function rng(seed) {
    let a = (seed >>> 0) || 1;
    const next = () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    return {
      next,
      // Бокс-Мюллер: нормальное распределение из двух равномерных
      norm(sigma) {
        const u = Math.max(next(), 1e-9), v = next();
        return sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
      },
    };
  }

  // --- DCT 8x8 -------------------------------------------------------------

  const D = (() => {
    const m = new Float32Array(64);
    for (let k = 0; k < 8; k++) {
      const c = (k === 0 ? Math.sqrt(1 / 8) : Math.sqrt(2 / 8));
      for (let n = 0; n < 8; n++) m[k * 8 + n] = c * Math.cos(Math.PI * (2 * n + 1) * k / 16);
    }
    return m;
  })();

  const QTABLE = new Float32Array([
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
  ]);

  /* Грубое квантование. Шаг по DC ограничен: в реальном кодеке средний
     уровень блока передаётся точно, битов лишается детализация. Без этого
     на высоких qp картинка распадается на несколько кислотных уровней. */
  function quantize(p, w, h, qp, cutoff, dcMax) {
    if (qp <= 0 && cutoff >= 14) return;
    const q = new Float32Array(64);
    for (let i = 0; i < 64; i++) q[i] = QTABLE[i] * (1 + qp * 42);
    q[0] = Math.min(q[0], dcMax);

    const blk = new Float32Array(64), t1 = new Float32Array(64), t2 = new Float32Array(64);

    for (let by = 0; by < h; by += 8) {
      for (let bx = 0; bx < w; bx += 8) {
        for (let y = 0; y < 8; y++)
          for (let x = 0; x < 8; x++) blk[y * 8 + x] = p[(by + y) * w + bx + x] - 128;

        for (let u = 0; u < 8; u++)
          for (let x = 0; x < 8; x++) {
            let s = 0;
            for (let n = 0; n < 8; n++) s += D[u * 8 + n] * blk[n * 8 + x];
            t1[u * 8 + x] = s;
          }
        for (let u = 0; u < 8; u++)
          for (let v = 0; v < 8; v++) {
            let s = 0;
            for (let x = 0; x < 8; x++) s += t1[u * 8 + x] * D[v * 8 + x];
            const qq = q[u * 8 + v];
            t2[u * 8 + v] = (u + v) <= cutoff ? Math.round(s / qq) * qq : 0;
          }
        for (let n = 0; n < 8; n++)
          for (let v = 0; v < 8; v++) {
            let s = 0;
            for (let u = 0; u < 8; u++) s += D[u * 8 + n] * t2[u * 8 + v];
            t1[n * 8 + v] = s;
          }
        for (let n = 0; n < 8; n++)
          for (let x = 0; x < 8; x++) {
            let s = 0;
            for (let v = 0; v < 8; v++) s += t1[n * 8 + v] * D[v * 8 + x];
            p[(by + n) * w + bx + x] = s + 128;
          }
      }
    }
  }

  // --- вспомогательное ------------------------------------------------------

  /* Кадр уезжает: сдвиг плюс лёгкий наплыв. По этому же пути едет ошибка. */
  function warp(src, w, h, dx, dy, zoom) {
    const out = new Float32Array(w * h);
    const xi = new Int32Array(w), yi = new Int32Array(h);
    for (let x = 0; x < w; x++)
      xi[x] = Math.min(w - 1, Math.max(0, Math.round((x - w / 2) * (1 - zoom) + w / 2 - dx)));
    for (let y = 0; y < h; y++)
      yi[y] = Math.min(h - 1, Math.max(0, Math.round((y - h / 2) * (1 - zoom) + h / 2 - dy)));
    for (let y = 0; y < h; y++) {
      const row = yi[y] * w, o = y * w;
      for (let x = 0; x < w; x++) out[o + x] = src[row + xi[x]];
    }
    return out;
  }

  /* Потери всплесками: p — уйти в плохое состояние, r — выйти из него. */
  function gilbertElliott(n, p, r, rnd) {
    const out = new Uint8Array(n);
    let bad = false;
    for (let i = 0; i < n; i++) {
      bad = bad ? rnd.next() > r : rnd.next() < p;
      out[i] = bad ? 1 : 0;
    }
    return out;
  }

  function packetLoss(totalMB, sliceLen, p, r, rnd) {
    const packets = Math.ceil(totalMB / sliceLen);
    const bad = gilbertElliott(packets, p, Math.max(1e-3, r), rnd);
    const lost = new Uint8Array(totalMB);
    for (let i = 0; i < totalMB; i++) lost[i] = bad[(i / sliceLen) | 0];
    return { lost, bad };
  }

  /* Копия блока из опорного кадра со смещением, края залипают. */
  function copyBlock(dst, src, w, h, y0, y1, x0, x1, dy, dx) {
    for (let y = y0; y < y1; y++) {
      const sy = Math.min(h - 1, Math.max(0, y + dy)) * w;
      const o = y * w;
      for (let x = x0; x < x1; x++) dst[o + x] = src[sy + Math.min(w - 1, Math.max(0, x + dx))];
    }
  }

  // --- основной проход -------------------------------------------------------

  function render(srcCanvas, prm) {
    const sub = 1 << Math.min(3, Math.max(1, prm.chromaSub | 0));
    const align = Math.max(MB, 8 * sub);
    const ow = srcCanvas.width, oh = srcCanvas.height;

    const w = Math.max(align, Math.round(prm.width / align) * align);
    const h = Math.max(align, Math.round((prm.width * oh / ow) / align) * align);

    // 1. приводим к разрешению потока
    const small = document.createElement('canvas');
    small.width = w; small.height = h;
    const sg = small.getContext('2d', { willReadFrequently: true });
    sg.imageSmoothingQuality = 'high';
    sg.drawImage(srcCanvas, 0, 0, w, h);
    const px = sg.getImageData(0, 0, w, h).data;

    const rnd = rng(prm.seed);

    // 2. RGB -> YCbCr, цвет прореживаем в sub раз
    const n = w * h, cw = w / sub, ch = h / sub;
    const baseY = new Float32Array(n);
    const baseCb = new Float32Array(cw * ch), baseCr = new Float32Array(cw * ch);
    const cCount = new Float32Array(cw * ch);
    const expo = prm.exposure ? 1 + prm.exposure * 1.6 : 0;

    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        let r = px[i], g = px[i + 1], b = px[i + 2];
        if (expo) {
          r = 255 * (1 - Math.pow(1 - r / 255, expo));
          g = 255 * (1 - Math.pow(1 - g / 255, expo));
          b = 255 * (1 - Math.pow(1 - b / 255, expo));
        }
        baseY[y * w + x] = 0.299 * r + 0.587 * g + 0.114 * b;
        const ci = ((y / sub) | 0) * cw + ((x / sub) | 0);
        baseCb[ci] += -0.168736 * r - 0.331264 * g + 0.5 * b + 128;
        baseCr[ci] += 0.5 * r - 0.418688 * g - 0.081312 * b + 128;
        cCount[ci]++;
      }
    }
    for (let i = 0; i < baseCb.length; i++) { baseCb[i] /= cCount[i]; baseCr[i] /= cCount[i]; }

    // цвет и так прорежен, поэтому давим его сильнее по деталям, мягче по DC
    quantize(baseY, w, h, prm.qp, prm.cutoff, 24);
    quantize(baseCb, cw, ch, prm.qp * 1.3, Math.max(0, prm.cutoff - 2), 14);
    quantize(baseCr, cw, ch, prm.qp * 1.3, Math.max(0, prm.cutoff - 2), 14);
    for (let i = 0; i < n; i++) baseY[i] = Math.min(255, Math.max(0, baseY[i]));
    for (let i = 0; i < baseCb.length; i++) {
      baseCb[i] = Math.min(255, Math.max(0, baseCb[i]));
      baseCr[i] = Math.min(255, Math.max(0, baseCr[i]));
    }

    // 3. накапливаем ошибку по кадрам
    const mbW = w / MB, totalMB = mbW * (h / MB);
    const sliceLen = Math.max(1, mbW >> 1);
    const cMB = MB / sub;

    let eY = new Float32Array(n);
    let eCb = new Float32Array(cw * ch), eCr = new Float32Array(cw * ch);
    let dxAcc = 0, dyAcc = 0, lastBad = null, lastPackets = 0;

    const curY = new Float32Array(n);
    const curCb = new Float32Array(cw * ch), curCr = new Float32Array(cw * ch);
    const prevY = new Float32Array(n);
    const prevCb = new Float32Array(cw * ch), prevCr = new Float32Array(cw * ch);

    for (let f = 0; f < Math.max(1, prm.frames); f++) {
      dxAcc += rnd.norm(prm.drift);
      dyAcc += rnd.norm(prm.drift);
      eY = warp(eY, w, h, dxAcc, dyAcc, prm.zoom);
      eCb = warp(eCb, cw, ch, dxAcc / sub, dyAcc / sub, prm.zoom);
      eCr = warp(eCr, cw, ch, dxAcc / sub, dyAcc / sub, prm.zoom);

      // intra-обновление чинит часть блоков начисто
      if (prm.intra > 0) {
        for (let mb = 0; mb < totalMB; mb++) {
          if (rnd.next() >= prm.intra) continue;
          const my = (mb / mbW) | 0, mx = mb % mbW;
          for (let y = my * MB; y < my * MB + MB; y++)
            eY.fill(0, y * w + mx * MB, y * w + mx * MB + MB);
          for (let y = my * cMB; y < my * cMB + cMB; y++) {
            eCb.fill(0, y * cw + mx * cMB, y * cw + mx * cMB + cMB);
            eCr.fill(0, y * cw + mx * cMB, y * cw + mx * cMB + cMB);
          }
        }
      }

      // то, что декодер показал бы, если бы всё доехало
      for (let i = 0; i < n; i++) prevY[i] = curY[i] = baseY[i] + eY[i];
      for (let i = 0; i < eCb.length; i++) {
        prevCb[i] = curCb[i] = baseCb[i] + eCb[i];
        prevCr[i] = curCr[i] = baseCr[i] + eCr[i];
      }

      // ...но часть пакетов не доехала
      const { lost, bad } = packetLoss(totalMB, sliceLen, prm.loss, prm.burst, rnd);
      lastBad = bad; lastPackets = bad.length;

      for (let mb = 0; mb < totalMB; mb++) {
        if (!lost[mb]) continue;
        const my = (mb / mbW) | 0, mx = mb % mbW;
        const y0 = my * MB, x0 = mx * MB;
        const cy0 = my * cMB, cx0 = mx * cMB;
        const mode = rnd.next();

        if (mode < prm.smear && y0 > 0) {
          // рисовать нечего — тянем последнюю удачную строку вниз
          for (let y = y0; y < y0 + MB; y++)
            for (let x = x0; x < x0 + MB; x++) curY[y * w + x] = curY[(y0 - 1) * w + x];
          if (cy0 > 0)
            for (let y = cy0; y < cy0 + cMB; y++)
              for (let x = cx0; x < cx0 + cMB; x++) {
                curCb[y * cw + x] = curCb[(cy0 - 1) * cw + x];
                curCr[y * cw + x] = curCr[(cy0 - 1) * cw + x];
              }
          continue;
        }

        let dy = 0, dx = 0;
        if (mode >= prm.smear + prm.freeze) { dy = rnd.norm(prm.mv) | 0; dx = rnd.norm(prm.mv) | 0; }
        copyBlock(curY, prevY, w, h, y0, y0 + MB, x0, x0 + MB, dy, dx);
        const cdy = (dy / sub) | 0, cdx = (dx / sub) | 0;
        copyBlock(curCb, prevCb, cw, ch, cy0, cy0 + cMB, cx0, cx0 + cMB, cdy, cdx);
        copyBlock(curCr, prevCr, cw, ch, cy0, cy0 + cMB, cx0, cx0 + cMB, cdy, cdx);
      }

      // цвет едет в своих пакетах и сыпется отдельно от яркости
      if (prm.chromaLoss > 1) {
        const extra = packetLoss(totalMB, sliceLen, Math.min(0.95, prm.loss * prm.chromaLoss), prm.burst, rnd).lost;
        for (let mb = 0; mb < totalMB; mb++) {
          if (!extra[mb] || lost[mb]) continue;
          const my = (mb / mbW) | 0, mx = mb % mbW;
          const cy0 = my * cMB, cx0 = mx * cMB;
          const d = (rnd.norm(prm.mv) / sub) | 0;
          copyBlock(curCb, prevCb, cw, ch, cy0, cy0 + cMB, cx0, cx0 + cMB, d, d);
          copyBlock(curCr, prevCr, cw, ch, cy0, cy0 + cMB, cx0, cx0 + cMB, -d, d);
        }
      }

      // призрак прошлого кадра поверх
      const g = prm.ghost;
      for (let i = 0; i < n; i++) {
        const v = g > 0 ? curY[i] * (1 - g) + prevY[i] * g : curY[i];
        eY[i] = Math.min(255, Math.max(0, v)) - baseY[i];
      }
      for (let i = 0; i < eCb.length; i++) {
        const b = g > 0 ? curCb[i] * (1 - g) + prevCb[i] * g : curCb[i];
        const r = g > 0 ? curCr[i] * (1 - g) + prevCr[i] * g : curCr[i];
        eCb[i] = Math.min(255, Math.max(0, b)) - baseCb[i];
        eCr[i] = Math.min(255, Math.max(0, r)) - baseCr[i];
      }
    }

    // 4. обратно в RGB
    const shift = Math.round(prm.chromaShift);
    const out = new ImageData(w, h);
    const od = out.data;
    for (let y = 0; y < h; y++) {
      const cy = (y / sub) | 0;
      for (let x = 0; x < w; x++) {
        const i = y * w + x;
        const cxb = Math.min(cw - 1, Math.max(0, ((x / sub) | 0) + shift));
        const cxr = Math.min(cw - 1, Math.max(0, ((x / sub) | 0) - shift));
        const Y = Math.min(255, Math.max(0, baseY[i] + eY[i]));
        const Cb = Math.min(255, Math.max(0, baseCb[cy * cw + cxb] + eCb[cy * cw + cxb])) - 128;
        const Cr = Math.min(255, Math.max(0, baseCr[cy * cw + cxr] + eCr[cy * cw + cxr])) - 128;
        const o = i * 4;
        od[o] = Y + 1.402 * Cr;
        od[o + 1] = Y - 0.344136 * Cb - 0.714136 * Cr;
        od[o + 2] = Y + 1.772 * Cb;
        od[o + 3] = 255;
      }
    }

    // шум сенсора
    if (prm.noise > 0) {
      for (let i = 0; i < od.length; i += 4) {
        const nz = rnd.norm(prm.noise);
        od[i] += nz; od[i + 1] += nz; od[i + 2] += nz;
      }
    }

    const stage = document.createElement('canvas');
    stage.width = w; stage.height = h;
    stage.getContext('2d').putImageData(out, 0, 0);

    // свечение в светах — мягкий пересвет дешёвой камеры
    const res = document.createElement('canvas');
    res.width = ow; res.height = oh;
    const rg = res.getContext('2d');
    rg.imageSmoothingEnabled = true;
    rg.imageSmoothingQuality = 'high';
    rg.drawImage(stage, 0, 0, ow, oh);   // апскейл билинейкой — то самое мыло
    if (prm.bloom > 0) {
      rg.save();
      rg.globalCompositeOperation = 'lighter';
      rg.globalAlpha = prm.bloom * 0.5;
      rg.filter = `blur(${Math.max(1, ow / 90).toFixed(1)}px) brightness(1.5) contrast(2.4)`;
      rg.drawImage(stage, 0, 0, ow, oh);
      rg.restore();
    }

    return { canvas: res, packets: lastBad, packetCount: lastPackets, width: w, height: h };
  }

  return { render };
})();
