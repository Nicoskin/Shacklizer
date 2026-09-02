/* Склейка интерфейса: загрузка картинки, ручки, очередь рендера. */

(() => {

const REPO = 'https://github.com/Nicoskin/Shacklizer';
const MAX_SIDE = 2000;                // больше нет смысла: это всё равно шакал

const $ = id => document.getElementById(id);
const el = {
  stage: $('stage'), view: $('view'), empty: $('empty'), badge: $('badge'),
  packets: $('packets'), stats: $('stats'), controls: $('controls'), presets: $('presets'),
  open: $('open'), file: $('file'), paste: $('pasteBtn'), reroll: $('reroll'), save: $('save'),
  engH264: $('eng-h264'), engSim: $('eng-sim'), engineNote: $('engineNote'),
  drop: $('drop'), loader: $('loader'), barFill: $('barFill'), barPct: $('barPct'),
  dot: $('dot'), status: $('status'), gh: $('gh'), hint: $('hint'),
};

const NOTES = {
  h264: 'Картинка правда кодируется x264, из потока выбрасываются NAL-юниты, и это доедает настоящий декодер. Первый запуск качает 32 МБ кодека.',
  sim: 'Своя модель поведения декодера при потерях. Считает мгновенно и ничего не качает, но артефакты придуманные.',
};

const state = {
  engine: 'h264',
  preset: DEFAULT_PRESET,
  seed: 7,
  params: { h264: {}, sim: {} },
  source: null,      // canvas с исходником
  name: 'jackal',
  result: null,
  busy: false,
  queued: false,
  showOriginal: false,
};

// ------------------------------------------------------------------ статус

function status(text, kind) {
  el.status.textContent = text;
  el.dot.className = 'dot' + (kind ? ' ' + kind : '');
}

// ------------------------------------------------------------------ ручки

function applyPreset(name) {
  state.preset = name;
  state.params.h264 = { ...PRESETS[name].h264 };
  state.params.sim = { ...PRESETS[name].sim };
  for (const b of el.presets.children) b.setAttribute('aria-pressed', String(b.textContent === name));
  buildControls();
  render();
}

function fmt(spec, v) {
  if (spec.pct) return Math.round(v * 100) + '%';
  const s = spec.step < 1 ? v.toFixed(1) : String(Math.round(v));
  return spec.unit ? `${s} ${spec.unit}` : s;
}

function buildControls() {
  const specs = SPECS[state.engine];
  const prm = state.params[state.engine];
  el.controls.innerHTML = '<h2>Настройки</h2>';

  for (const spec of specs) {
    const wrap = document.createElement('div');
    wrap.className = 'ctl';

    const label = document.createElement('label');
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = fmt(spec, prm[spec.key]);
    label.append(document.createTextNode(spec.label), num);

    const input = document.createElement('input');
    input.type = 'range';
    input.min = spec.min; input.max = spec.max; input.step = spec.step;
    input.value = prm[spec.key];
    input.addEventListener('input', () => {
      prm[spec.key] = parseFloat(input.value);
      num.textContent = fmt(spec, prm[spec.key]);
      render();
    });

    wrap.append(label, input);
    el.controls.append(wrap);
  }

  if (state.engine === 'h264') {
    const lab = document.createElement('label');
    lab.className = 'check';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = prm.heal !== false;
    cb.addEventListener('change', () => { prm.heal = cb.checked; render(); });
    lab.append(cb, document.createTextNode('Затягивать зелёные дыры декодера'));
    el.controls.append(lab);
  }
}

function setEngine(name) {
  state.engine = name;
  el.engH264.setAttribute('aria-pressed', String(name === 'h264'));
  el.engSim.setAttribute('aria-pressed', String(name === 'sim'));
  el.engineNote.textContent = NOTES[name];
  buildControls();
  render();
}

// ------------------------------------------------------------------ рендер

async function ensureCore() {
  if (H264.isReady()) return true;
  el.loader.classList.add('on');
  el.barFill.style.width = '0%';
  el.barPct.textContent = '0%';
  try {
    await H264.load(p => {
      const pct = Math.round(p * 100);
      el.barFill.style.width = pct + '%';
      el.barPct.textContent = pct + '%';
    });
    return true;
  } catch (e) {
    status('Не удалось загрузить кодек: ' + e.message + ' — переключитесь на симуляцию', 'err');
    return false;
  } finally {
    el.loader.classList.remove('on');
  }
}

async function render() {
  if (!state.source) return;
  if (state.busy) { state.queued = true; return; }
  state.busy = true;
  state.queued = false;

  const engine = state.engine;
  const prm = { ...state.params[engine], seed: state.seed };

  try {
    // Пока качается 32-мегабайтное ядро, показываем быструю симуляцию,
    // чтобы окно не стояло пустым.
    if (engine === 'h264' && !H264.isReady()) {
      show(Sim.render(state.source, { ...state.params.sim, seed: state.seed }), 'sim', true);
      status('Качаем кодек, пока показываем симуляцию…', 'busy');
      if (!await ensureCore()) return;
    }

    status('Считаем…', 'busy');
    const t0 = performance.now();
    const out = engine === 'h264'
      ? await H264.render(state.source, prm)
      : Sim.render(state.source, prm);
    show(out, engine, false, performance.now() - t0);
  } catch (e) {
    console.error(e);
    status('Ошибка: ' + (e && e.message ? e.message : e), 'err');
  } finally {
    state.busy = false;
    if (state.queued) render();
  }
}

function show(out, engine, provisional, ms) {
  state.result = out.canvas;
  el.save.disabled = false;
  el.reroll.disabled = false;
  draw();
  drawPackets(out.packets, engine);

  const parts = [];
  if (engine === 'h264') {
    const pct = out.packetCount ? Math.round(100 * (out.dropped + out.corrupted) / out.packetCount) : 0;
    parts.push(['пакетов', out.packetCount]);
    parts.push(['потеряно', `${out.dropped} (${pct}%)`, 'loss']);
    parts.push(['битых', out.corrupted, 'loss']);
    parts.push(['поток', (out.bytes / 1024).toFixed(1) + ' КБ']);
    parts.push(['кадров', out.decoded, 'ok']);
  } else {
    const lostN = out.packets ? out.packets.reduce((a, v) => a + v, 0) : 0;
    const pct = out.packetCount ? Math.round(100 * lostN / out.packetCount) : 0;
    parts.push(['пакетов в кадре', out.packetCount]);
    parts.push(['потеряно', `${lostN} (${pct}%)`, 'loss']);
  }
  parts.push(['кадр', `${out.width}×${out.height}`]);

  el.stats.innerHTML = parts
    .map(([k, v, c]) => `<span><b>${k}</b> <span class="v ${c || ''}">${v}</span></span>`)
    .join('');

  if (!provisional) status(`Готово за ${(ms / 1000).toFixed(2)} с`, 'ok');
}

function draw() {
  const img = state.showOriginal ? state.source : state.result;
  if (!img) return;
  el.view.classList.remove('hidden');
  el.empty.style.display = 'none';
  el.view.width = img.width;
  el.view.height = img.height;
  el.view.getContext('2d').drawImage(img, 0, 0);
}

let lastMarks = null, lastEngine = 'h264';

/* Полоска пакетов: каждая чёрточка — один NAL-юнит. Серые доехали,
   красные потерялись, жёлтые доехали битыми. */
function drawPackets(marks, engine) {
  if (marks !== undefined) { lastMarks = marks; lastEngine = engine; }
  const c = el.packets;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  if (!lastMarks || !lastMarks.length) return;

  // для симуляции приходит маска «плохих», приводим к общему виду
  const list = lastEngine === 'sim' ? Array.from(lastMarks, v => (v ? 0 : 1)) : lastMarks;
  const n = list.length;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = list[i];
    g.fillStyle = v === 1 ? '#2b3040' : v === 2 ? '#ffb347' : '#ff3d6e';
    const x = i * bw;
    g.fillRect(x, v === 1 ? 9 : 4, Math.max(1, bw - (bw > 3 ? 1 : 0)), v === 1 ? 8 : 18);
  }
}

// ------------------------------------------------------------------ файлы

function setSource(img, name) {
  const scale = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(img.width * scale));
  c.height = Math.max(1, Math.round(img.height * scale));
  const g = c.getContext('2d');
  g.imageSmoothingQuality = 'high';
  g.drawImage(img, 0, 0, c.width, c.height);
  state.source = c;
  state.name = (name || 'jackal').replace(/\.[^.]+$/, '');
  render();
}

async function loadBlob(blob, name) {
  try {
    // from-image — чтобы не потерять поворот из EXIF у фоток с телефона
    const bmp = await createImageBitmap(blob, { imageOrientation: 'from-image' });
    setSource(bmp, name);
  } catch (e) {
    status('Не получилось открыть файл: ' + e.message, 'err');
  }
}

function pickFile(files) {
  const img = [...files].find(x => x.type.startsWith('image/'));
  if (img) loadBlob(img, img.name);
  else status('Это не картинка', 'err');
}

// ------------------------------------------------------------------ события

el.open.addEventListener('click', () => el.file.click());
el.file.addEventListener('change', () => { if (el.file.files.length) pickFile(el.file.files); el.file.value = ''; });

el.paste.addEventListener('click', async () => {
  try {
    const items = await navigator.clipboard.read();
    for (const it of items) {
      const type = it.types.find(t => t.startsWith('image/'));
      if (type) return loadBlob(await it.getType(type), 'clipboard');
    }
    status('В буфере нет картинки', 'err');
  } catch {
    status('Браузер не дал доступ к буферу — нажмите Ctrl+V', 'err');
  }
});

window.addEventListener('paste', e => {
  const item = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith('image/'));
  if (item) { e.preventDefault(); loadBlob(item.getAsFile(), 'clipboard'); }
});

let dragDepth = 0;
window.addEventListener('dragenter', e => { e.preventDefault(); if (++dragDepth === 1) el.drop.classList.add('on'); });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; el.drop.classList.remove('on'); } });
window.addEventListener('drop', e => {
  e.preventDefault();
  dragDepth = 0;
  el.drop.classList.remove('on');
  if (e.dataTransfer?.files?.length) pickFile(e.dataTransfer.files);
});

el.reroll.addEventListener('click', () => {
  state.seed = (Math.imul(state.seed, 1103515245) + 12345) >>> 0 || 1;
  render();
});

el.save.addEventListener('click', () => {
  if (!state.result) return;
  state.result.toBlob(blob => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.name}_${state.preset.replace(/\s+/g, '_')}.jpg`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, 'image/jpeg', 0.92);
});

el.engH264.addEventListener('click', () => setEngine('h264'));
el.engSim.addEventListener('click', () => setEngine('sim'));

// зажать картинку — показать оригинал
const hold = on => {
  if (!state.source || !state.result) return;
  state.showOriginal = on;
  el.badge.classList.toggle('on', on);
  draw();
};
el.view.addEventListener('pointerdown', e => { e.preventDefault(); hold(true); });
window.addEventListener('pointerup', () => hold(false));
window.addEventListener('pointercancel', () => hold(false));

window.addEventListener('resize', () => drawPackets());

// ------------------------------------------------------------------ старт

for (const name of Object.keys(PRESETS)) {
  const b = document.createElement('button');
  b.className = 'chip';
  b.textContent = name;
  b.setAttribute('aria-pressed', String(name === DEFAULT_PRESET));
  b.addEventListener('click', () => applyPreset(name));
  el.presets.append(b);
}

if (REPO === 'https://github.com/') el.gh.style.display = 'none';
else el.gh.href = REPO;

H264.setNotice(msg => status(msg, 'busy'));

if (!H264.supported()) {
  el.engH264.disabled = true;
  state.engine = 'sim';
}

state.params.h264 = { ...PRESETS[DEFAULT_PRESET].h264 };
state.params.sim = { ...PRESETS[DEFAULT_PRESET].sim };
setEngine(state.engine);
status('Откройте фотографию');

})();
