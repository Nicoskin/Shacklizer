/* Движок «Настоящий H.264» — ffmpeg.wasm прямо в браузере.

   Тут ничего не имитируется. Картинка реально кодируется x264, поток
   режется на NAL-юниты (один NAL = один слайс = одна RTP-посылка = один
   UDP-пакет), часть юнитов выбрасывается или бьётся побайтно, и это
   скармливается настоящему декодеру с включённым error concealment.
   Артефакты получаются ровно те, что рисует живой декодер.

   Ядро (32 МБ wasm) тянется с CDN один раз и кэшируется браузером.
   Склейка (vendor/ffmpeg.js) лежит локально: воркер нельзя создать с
   чужого домена. */

const H264 = (() => {

  const CORE = 'https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.10/dist/umd';
  const KEEP_FRAMES = 12;   // сколько последних кадров забираем из декодера

  /* Распакованный размер ffmpeg-core.wasm 0.12.10. CDN отдаёт файл сжатым,
     и content-length — это размер сжатого, по нему прогресс улетает за 100%.
     Если версия сменится, разойдётся только скорость бегунка: он всё равно
     ограничен сверху и в конце доводится до 100%. */
  const CORE_BYTES = 32232419;

  let ff = null;
  let loading = null;
  let logTail = [];

  const supported = () => typeof FFmpegWASM !== 'undefined' && typeof WebAssembly !== 'undefined';

  /* @ffmpeg/util в браузере падает (лезет к `exports`), а нужна из него
     ровно одна функция — вот она. */
  async function toBlobURL(url, type, onProgress) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status} ${url}`);
    if (!onProgress || !res.body) {
      return URL.createObjectURL(new Blob([await res.arrayBuffer()], { type }));
    }
    const total = Math.max(+res.headers.get('content-length') || 0, CORE_BYTES);
    const reader = res.body.getReader();
    const chunks = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.length;
      onProgress(Math.min(0.99, got / total));
    }
    onProgress(1);
    return URL.createObjectURL(new Blob(chunks, { type }));
  }

  function load(onProgress) {
    if (loading) return loading;
    loading = (async () => {
      const { FFmpeg } = FFmpegWASM;
      ff = new FFmpeg();
      ff.on('log', ({ message }) => {
        logTail.push(message);
        if (logTail.length > 60) logTail.shift();
      });
      const [coreURL, wasmURL] = [
        await toBlobURL(`${CORE}/ffmpeg-core.js`, 'text/javascript'),
        await toBlobURL(`${CORE}/ffmpeg-core.wasm`, 'application/wasm', onProgress),
      ];
      await ff.load({ coreURL, wasmURL });
      return ff;
    })();
    loading.catch(() => { loading = null; });
    return loading;
  }

  // ---------------------------------------------------------------- поток

  /* Разбить Annex-B поток на NAL-юниты вместе со стартовыми кодами. */
  function splitNals(buf) {
    const nals = [];
    let start = -1;
    for (let i = 0; i + 2 < buf.length; i++) {
      if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) {
        const s = (i > 0 && buf[i - 1] === 0) ? i - 1 : i;
        if (start >= 0) nals.push([start, s]);
        start = s;
        i += 2;
      }
    }
    if (start >= 0) nals.push([start, buf.length]);
    return nals;
  }

  function nalType(buf, from) {
    let s = from;
    while (s < buf.length && buf[s] === 0) s++;
    return s + 1 < buf.length ? buf[s + 1] & 0x1f : 0;
  }

  /* Выкинуть или побить часть NAL-юнитов. Заголовки и первый IDR не
     трогаем — иначе декодеру не от чего отталкиваться. */
  function dropPackets(buf, prm, rnd) {
    const nals = splitNals(buf);
    const keep = [];
    const marks = [];           // 1 = доехал, 0 = потерян, 2 = доехал битым
    let bad = false, seenIdr = false, dropped = 0, corrupted = 0;

    for (const [a, b] of nals) {
      const t = nalType(buf, a);
      if (t === 7 || t === 8 || t === 9 || t === 6 || (t === 5 && !seenIdr)) {
        if (t === 5) seenIdr = true;
        keep.push([a, b, false]);
        continue;
      }
      bad = bad ? rnd() > prm.burst : rnd() < prm.loss;
      if (!bad) { keep.push([a, b, false]); marks.push(1); continue; }
      if (rnd() < prm.corrupt && b - a > 16) {
        keep.push([a, b, true]);       // пакет доехал битым: CRC нет, декодер рисует мусор
        marks.push(2); corrupted++;
      } else {
        marks.push(0); dropped++;      // пакет просто потерян
      }
    }

    let size = 0;
    for (const [a, b] of keep) size += b - a;
    const out = new Uint8Array(size);
    let o = 0;
    for (const [a, b, hit] of keep) {
      out.set(buf.subarray(a, b), o);
      if (hit) {
        const len = b - a;
        const hits = Math.max(1, len / 40 | 0);
        for (let k = 0; k < hits; k++) {
          const j = o + 8 + ((rnd() * (len - 8)) | 0);
          if (j < o + len) out[j] ^= 1 + ((rnd() * 255) | 0);
        }
      }
      o += b - a;
    }
    return { data: out, marks, dropped, corrupted, total: marks.length };
  }

  /* Ядовито-зелёные пятна — макроблоки, до которых декодер не дошёл, там
     осталась неинициализированная YUV-нулёвка. Нормальный плеер показал бы
     последнюю удачную картинку, что мы и делаем. */
  function heal(frames, idx, w, h) {
    const cur = frames[idx].slice();
    const isHole = (buf, i) => {
      const r = buf[i], g = buf[i + 1], b = buf[i + 2];
      return g - Math.max(r, b) > 60 && r < 110 && b < 110;
    };
    let holes = 0;
    for (let i = 0; i < cur.length; i += 4) if (isHole(cur, i)) holes++;
    if (!holes) return cur;
    for (let f = idx - 1; f >= 0 && holes > 0; f--) {
      const prev = frames[f];
      for (let i = 0; i < cur.length; i += 4) {
        if (!isHole(cur, i) || isHole(prev, i)) continue;
        cur[i] = prev[i]; cur[i + 1] = prev[i + 1]; cur[i + 2] = prev[i + 2];
        holes--;
      }
    }
    return cur;
  }

  // ---------------------------------------------------------------- проход

  async function cleanup(names) {
    for (const nm of names) {
      try { await ff.deleteFile(nm); } catch { /* файла может не быть */ }
    }
  }

  async function render(srcCanvas, prm) {
    if (!ff) throw new Error('кодек ещё не загружен');

    const ow = srcCanvas.width, oh = srcCanvas.height;
    const even = v => Math.max(64, Math.round(v / 2) * 2);
    const w = even(prm.width);
    const h = even(w * oh / ow);
    const m = Math.round(prm.motion);
    const bigW = even(w + 2 * m), bigH = even(h + 2 * m);

    // Кодируем уже готовый кадр нужного размера: ffmpeg остаётся только
    // ездить по нему кропом, а не масштабировать многомегапиксельный PNG.
    const pre = document.createElement('canvas');
    pre.width = bigW; pre.height = bigH;
    const pg = pre.getContext('2d');
    pg.imageSmoothingQuality = 'high';
    const cover = Math.max(bigW / ow, bigH / oh);
    pg.drawImage(srcCanvas, (bigW - ow * cover) / 2, (bigH - oh * cover) / 2, ow * cover, oh * cover);
    const png = new Uint8Array(await (await new Promise(r => pre.toBlob(r, 'image/png'))).arrayBuffer());

    await cleanup(['in.png', 'raw.h264', 'bad.h264', 'out.raw', 'count.raw']);
    await ff.writeFile('in.png', png);

    // 1. кодируем; дрожание камеры даёт настоящие векторы движения,
    //    один поток — чтобы seed что-то значил
    const frames = Math.max(2, prm.frames | 0);
    const refresh = Math.max(2, prm.refresh | 0) || 9999;
    logTail = [];
    await ff.exec([
      '-loop', '1', '-i', 'in.png', '-frames:v', String(frames), '-r', '15',
      '-vf', `crop=${w}:${h}:'(in_w-out_w)/2+${m}*sin(n/7)':'(in_h-out_h)/2+${m}*cos(n/9)'`,
      '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
      '-b:v', `${prm.bitrate}k`, '-maxrate', `${prm.bitrate}k`, '-bufsize', `${prm.bitrate * 2}k`,
      '-bf', '0', '-threads', '1', '-g', String(refresh),
      // intra-refresh — то, чем реальные видеозвонки лечатся от потерь:
      // по кадру ползёт полоса intra-блоков и постепенно чинит картинку.
      // Чем длиннее период, тем дольше живёт однажды заехавший артефакт.
      '-x264-params', `slices=${Math.max(1, prm.slices | 0)}:scenecut=0:` +
        `intra-refresh=1:keyint=${refresh}:threads=1:sliced-threads=0`,
      '-f', 'h264', 'raw.h264',
    ]);
    const raw = await ff.readFile('raw.h264');
    if (!raw.length) throw new Error('кодировщик ничего не выдал: ' + logTail.slice(-3).join(' | '));

    // 2. теряем пакеты
    const lost = dropPackets(raw, prm, rngFrom(prm.seed));
    await ff.writeFile('bad.h264', lost.data);

    // 3. декодируем как есть — пусть выкручивается.
    //    Сколько кадров переживёт потери, заранее неизвестно (битые кадры
    //    декодер может и не отдать), поэтому сначала считаем их дешёвым
    //    проходом в 8x8, а уже потом забираем последние KEEP_FRAMES.
    const DECODE = [
      '-err_detect', 'ignore_err', '-ec', 'guess_mvs+deblock+favor_inter',
      '-flags2', '+showall', '-i', 'bad.h264',
    ];
    logTail = [];
    await ff.exec([...DECODE, '-vf', 'scale=8:8', '-f', 'rawvideo', '-pix_fmt', 'gray', 'count.raw']);
    const total = (await ff.readFile('count.raw')).length / 64;
    if (!total) throw new Error('декодер не выдал кадров — уменьшите потери');

    const start = Math.max(0, Math.floor(total) - KEEP_FRAMES);
    await ff.exec([...DECODE, '-vf', `trim=start_frame=${start}`,
      '-f', 'rawvideo', '-pix_fmt', 'rgba', 'out.raw']);
    const rawOut = await ff.readFile('out.raw');
    const frameSize = w * h * 4;
    const count = Math.floor(rawOut.length / frameSize);
    if (!count) throw new Error('декодер не выдал кадров — уменьшите потери');

    const list = [];
    for (let i = 0; i < count; i++) list.push(rawOut.subarray(i * frameSize, (i + 1) * frameSize));

    const idx = Math.min(count - 1, Math.max(0, count - 1 - (prm.grab | 0)));
    const pixels = prm.heal === false ? list[idx].slice() : heal(list, idx, w, h);

    await cleanup(['in.png', 'raw.h264', 'bad.h264', 'out.raw', 'count.raw']);

    const stage = document.createElement('canvas');
    stage.width = w; stage.height = h;
    stage.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(pixels.buffer.slice(pixels.byteOffset, pixels.byteOffset + pixels.byteLength)), w, h), 0, 0);

    const res = document.createElement('canvas');
    res.width = ow; res.height = oh;
    const rg = res.getContext('2d');
    rg.imageSmoothingEnabled = true;
    rg.imageSmoothingQuality = 'high';
    rg.drawImage(stage, 0, 0, ow, oh);   // апскейл билинейкой — мыло растянутого потока

    return {
      canvas: res,
      packets: lost.marks,
      packetCount: lost.total,
      dropped: lost.dropped,
      corrupted: lost.corrupted,
      bytes: raw.length,
      decoded: count,
      width: w,
      height: h,
    };
  }

  function rngFrom(seed) {
    let a = (seed >>> 0) || 1;
    return () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  return { supported, load, render, isReady: () => !!ff };
})();
