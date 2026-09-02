"""Шакалайзер — окно с превью и ручками.

Запуск:  python app.py [файл]
"""

from __future__ import annotations

import dataclasses
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from jackal import ffmpeg_engine as ff
from jackal.core import Params, degrade
from jackal.presets import DEFAULT, FF_PRESETS, PRESETS

IMAGE_TYPES = [
    ("Картинки", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
    ("Все файлы", "*.*"),
]

# (атрибут, подпись, min, max, шаг)  — шаг 1 без дробной части = целое
CORE_SPECS: list[tuple[str, list[tuple]]] = [
    ("Канал", [
        ("width", "Разрешение потока, px", 96, 720, 16),
        ("frames", "Кадров болтанки", 1, 40, 1),
        ("loss", "Потеря пакетов", 0.0, 0.6, 0.01),
        ("burst", "Восстановление после потери", 0.05, 1.0, 0.01),
        ("slice_len", "Макроблоков в пакете (0 = авто)", 0, 60, 1),
    ]),
    ("Декодер", [
        ("intra", "Intra-обновление", 0.0, 0.6, 0.005),
        ("mv", "Разброс векторов движения, px", 0.0, 40.0, 0.5),
        ("drift", "Дрейф кадра, px", 0.0, 12.0, 0.1),
        ("zoom", "Наплыв кадра", 0.0, 0.04, 0.001),
        ("smear", "Вертикальный размаз", 0.0, 1.0, 0.01),
        ("freeze", "Заморозка блока", 0.0, 1.0, 0.01),
        ("ghost", "Призрак прошлого кадра", 0.0, 0.6, 0.01),
    ]),
    ("Кодек", [
        ("qp", "Квантование", 0.0, 1.0, 0.01),
        ("cutoff", "Срез высоких частот", 0, 14, 1),
        ("chroma_sub", "Огрубление цвета", 1, 3, 1),
        ("chroma_loss", "Потери цвета, x", 1.0, 3.0, 0.05),
        ("chroma_shift", "Разъезд цвета, px", 0.0, 10.0, 0.5),
    ]),
    ("Камера и вывод", [
        ("exposure", "Пересвет", 0.0, 0.8, 0.01),
        ("bloom", "Свечение", 0.0, 1.0, 0.01),
        ("noise", "Шум", 0.0, 10.0, 0.1),
        ("jpeg", "Финальный JPEG (0 = выкл)", 0, 95, 1),
    ]),
]

FF_SPECS: list[tuple[str, list[tuple]]] = [
    ("Кодирование", [
        ("width", "Разрешение потока, px", 96, 720, 2),
        ("frames", "Длина клипа, кадров", 4, 150, 1),
        ("bitrate", "Битрейт, kbit/s", 10, 600, 5),
        ("slices", "Слайсов на кадр", 1, 16, 1),
        ("refresh", "Период intra-обновления, кадров", 2, 64, 1),
        ("motion", "Движение камеры, px", 0.0, 80.0, 1.0),
    ]),
    ("Сеть", [
        ("loss", "Потеря пакетов", 0.0, 0.6, 0.01),
        ("burst", "Восстановление после потери", 0.05, 1.0, 0.01),
        ("corrupt", "Доля битых вместо потерянных", 0.0, 1.0, 0.05),
        ("grab", "Какой кадр забрать (-1 = последний)", -1, 149, 1),
    ]),
]


class ScrollFrame(ttk.Frame):
    """Вертикально прокручиваемый контейнер — ручек много."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=350)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)

        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _wheel(self, event):
        self.canvas.yview_scroll(-event.delta // 120, "units")


class App:
    def __init__(self, root: tk.Tk, path: str | None = None):
        self.root = root
        root.title("Шакалайзер — потеря пакетов на видеосвязи")
        root.geometry("1180x820")
        root.minsize(900, 600)

        self.source: Image.Image | None = None
        self.source_path: Path | None = None
        self.result: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._showing_original = False

        self.engine = tk.StringVar(value="core")
        self.preset = tk.StringVar(value=DEFAULT)
        self.seed = tk.IntVar(value=7)
        self.lock_seed = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Откройте картинку")

        self.vars: dict[str, tk.Variable] = {}
        self.ff_vars: dict[str, tk.Variable] = {}

        self._build()

        # Рендер живёт в отдельном потоке: слайдеры не должны залипать.
        # Результаты забирает главный поток — трогать Tk из чужого нельзя.
        self._jobs: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._token = 0
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(40, self._poll)

        self.apply_preset()
        if path:
            self.load(Path(path))

    # ---------------------------------------------------------------- вёрстка

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")

        ttk.Button(top, text="Открыть…", command=self.open).pack(side="left")
        ttk.Button(top, text="Из буфера", command=self.paste).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Сохранить…", command=self.save).pack(side="left", padx=(6, 0))

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Label(top, text="Пресет:").pack(side="left")
        box = ttk.Combobox(top, textvariable=self.preset, values=list(PRESETS),
                           state="readonly", width=16)
        box.pack(side="left", padx=(6, 0))
        box.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=12)

        ttk.Label(top, text="Движок:").pack(side="left")
        ttk.Radiobutton(top, text="симуляция", value="core", variable=self.engine,
                        command=self._switch_engine).pack(side="left", padx=(6, 0))
        rb = ttk.Radiobutton(top, text="настоящий H.264", value="ffmpeg",
                             variable=self.engine, command=self._switch_engine)
        rb.pack(side="left", padx=(6, 0))
        if not ff.available():
            rb.state(["disabled"])

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(top, text="Новый бросок", command=self.reroll).pack(side="left")
        ttk.Checkbutton(top, text="фикс. seed", variable=self.lock_seed).pack(side="left", padx=(6, 0))

        # --- основная область ---
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, padding=(10, 0, 5, 0))
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, bg="#1b1b1e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<ButtonPress-1>", lambda e: self._toggle_original(True))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._toggle_original(False))

        hint = ttk.Label(left, text="Зажмите картинку мышью, чтобы увидеть оригинал",
                         foreground="#888")
        hint.pack(anchor="w", pady=(4, 6))

        self.panel = ScrollFrame(main)
        self.panel.pack(side="right", fill="y")

        self.core_box = ttk.Frame(self.panel.body)
        self.ff_box = ttk.Frame(self.panel.body)
        self._fill(self.core_box, CORE_SPECS, self.vars)
        self._fill(self.ff_box, FF_SPECS, self.ff_vars)

        heal = tk.BooleanVar(value=True)
        self.ff_vars["heal"] = heal
        ttk.Checkbutton(self.ff_box, text="Затягивать зелёные дыры декодера",
                        variable=heal, command=self.schedule).pack(anchor="w", padx=16)
        self.core_box.pack(fill="x")

        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.status).pack(side="left")

    def _fill(self, parent, specs, store) -> None:
        for section, rows in specs:
            group = ttk.LabelFrame(parent, text=section, padding=(8, 4))
            group.pack(fill="x", padx=8, pady=6)
            for attr, label, lo, hi, step in rows:
                is_int = isinstance(step, int) and isinstance(lo, int)
                var = tk.IntVar() if is_int else tk.DoubleVar()
                store[attr] = var
                ttk.Label(group, text=label).pack(anchor="w")
                tk.Scale(group, from_=lo, to=hi, resolution=step, variable=var,
                         orient="horizontal", showvalue=True, length=300,
                         command=lambda _v: self.schedule()).pack(fill="x", pady=(0, 4))

    def _switch_engine(self) -> None:
        self.core_box.pack_forget()
        self.ff_box.pack_forget()
        (self.core_box if self.engine.get() == "core" else self.ff_box).pack(fill="x")
        self.schedule()

    # ------------------------------------------------------------- параметры

    def apply_preset(self) -> None:
        p = PRESETS[self.preset.get()]
        for attr, var in self.vars.items():
            var.set(getattr(p, attr))

        # У движков разные ручки, поэтому и наборы настроек разные.
        f = FF_PRESETS[self.preset.get()]
        for attr, var in self.ff_vars.items():
            var.set(getattr(f, attr))

        self.schedule()

    def params(self) -> Params:
        p = Params(**{a: v.get() for a, v in self.vars.items()})
        p.seed = self.seed.get() if self.lock_seed.get() else None
        return p

    def ff_params(self) -> ff.FFParams:
        p = ff.FFParams(**{a: v.get() for a, v in self.ff_vars.items()})
        p.seed = self.seed.get() if self.lock_seed.get() else None
        return p

    def reroll(self) -> None:
        self.seed.set((self.seed.get() * 1103515245 + 12345) % 2147483647)
        self.schedule()

    # ---------------------------------------------------------------- рендер

    def schedule(self) -> None:
        """Слайдеры дёргаются часто — рендерим только последнее состояние."""
        if self.source is None:
            return
        self._token += 1
        token = self._token
        engine = self.engine.get()
        args = self.ff_params() if engine == "ffmpeg" else self.params()
        self.status.set("Считаю…")
        self._jobs.put((token, engine, self.source, args))

    def _worker(self) -> None:
        while True:
            token, engine, img, args = self._jobs.get()
            # если в очереди уже есть свежее задание — это пропускаем
            if token != self._token:
                continue
            try:
                out = ff.degrade(img, args) if engine == "ffmpeg" else degrade(img, args)
                err = None
            except Exception as exc:  # показываем, а не молчим
                out, err = None, f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            self._results.put((token, out, err))

    def _poll(self) -> None:
        try:
            while True:
                self._done(*self._results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    def _done(self, token: int, out: Image.Image | None, err: str | None) -> None:
        if token != self._token:
            return
        if err:
            self.status.set(err)
            return
        self.result = out
        w, h = out.size
        self.status.set(f"{self.source_path.name if self.source_path else 'буфер'} — {w}x{h}")
        self._redraw()

    # ---------------------------------------------------------------- превью

    def _toggle_original(self, on: bool) -> None:
        self._showing_original = on
        self._redraw()

    def _redraw(self) -> None:
        img = self.source if self._showing_original else self.result
        if img is None:
            return
        cw = max(1, self.canvas.winfo_width())
        cha = max(1, self.canvas.winfo_height())
        scale = min(cw / img.width, cha / img.height)
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        # NEAREST при увеличении, чтобы не мылить артефакты сверх меры
        resample = Image.NEAREST if scale > 1.2 else Image.LANCZOS
        self._photo = ImageTk.PhotoImage(img.resize(size, resample))
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, cha // 2, image=self._photo)

    # ----------------------------------------------------------------- файлы

    def open(self) -> None:
        name = filedialog.askopenfilename(title="Открыть картинку", filetypes=IMAGE_TYPES)
        if name:
            self.load(Path(name))

    def load(self, path: Path) -> None:
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            messagebox.showerror("Не открылось", str(exc))
            return
        self.source = img.convert("RGB")
        self.source_path = path
        self.schedule()

    def paste(self) -> None:
        try:
            from PIL import ImageGrab
            data = ImageGrab.grabclipboard()
        except Exception as exc:
            messagebox.showerror("Буфер обмена", str(exc))
            return
        if isinstance(data, list) and data:
            self.load(Path(data[0]))
            return
        if isinstance(data, Image.Image):
            self.source = data.convert("RGB")
            self.source_path = None
            self.schedule()
            return
        messagebox.showinfo("Буфер обмена", "В буфере нет картинки")

    def save(self) -> None:
        if self.result is None:
            return
        stem = self.source_path.stem if self.source_path else "jackal"
        suggested = f"{stem}_{self.preset.get().replace(' ', '_')}.jpg"
        name = filedialog.asksaveasfilename(
            title="Сохранить", initialfile=suggested, defaultextension=".jpg",
            filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png")])
        if not name:
            return
        out = self.result
        if name.lower().endswith((".jpg", ".jpeg")):
            out.save(name, quality=92, subsampling=1)
        else:
            out.save(name)
        self.status.set(f"Сохранено: {name}")


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
