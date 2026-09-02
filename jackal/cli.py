"""Командный интерфейс: python -m jackal ...

    python -m jackal photo.jpg
    python -m jackal photo.jpg -p jackal -o out.jpg
    python -m jackal photo.jpg -n 6                 # шесть вариантов подряд
    python -m jackal ./album -o ./out -p broken     # пачкой
    python -m jackal photo.jpg -e ffmpeg            # настоящий H.264
    python -m jackal photo.jpg --set loss=0.3 --set frames=20
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from PIL import Image

from . import ffmpeg_engine as ff
from .core import Params, degrade
from .presets import ALIASES, DEFAULT, PRESETS, resolve, resolve_ff

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _coerce(current, text: str):
    """Привести значение из --set к типу поля."""
    if isinstance(current, bool):
        return text.lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(text))
    if isinstance(current, float):
        return float(text)
    return text


def _apply_overrides(params, pairs: list[str]):
    fields = {f.name: f for f in dataclasses.fields(params)}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip().replace("-", "_")
        if key not in fields:
            raise SystemExit(f"unknown parameter {key!r}; known: {', '.join(fields)}")
        setattr(params, key, _coerce(getattr(params, key), value.strip()))
    return params


def _inputs(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(f for f in path.iterdir() if f.suffix.lower() in EXTS)
    return [path]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jackal",
        description="Degrade photos as if received over a lossy video call.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="presets: " + ", ".join(f"{a} ({r})" for a, r in ALIASES.items()),
    )
    ap.add_argument("input", nargs="?", type=Path, help="image file or directory")
    ap.add_argument("-o", "--output", type=Path, help="output file or directory")
    ap.add_argument("-p", "--preset", default=DEFAULT, help="preset name or alias")
    ap.add_argument("-e", "--engine", choices=("core", "ffmpeg"), default="core",
                    help="core = simulation (default), ffmpeg = real H.264 packet loss")
    ap.add_argument("-n", "--variants", type=int, default=1,
                    help="how many differently-seeded variants per image")
    ap.add_argument("--seed", type=int, help="fixed seed (default: random)")
    ap.add_argument("--width", type=int, help="stream resolution override")
    ap.add_argument("--out-width", type=int, help="output width (default: source width)")
    ap.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="KEY=VALUE", help="override any parameter, repeatable")
    ap.add_argument("--list", action="store_true", help="list presets and exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    if args.list:
        from .presets import FF_PRESETS
        for alias, russian in ALIASES.items():
            p, f = PRESETS[russian], FF_PRESETS[russian]
            print(f"{alias:10s} {russian:14s} "
                  f"sim: loss={p.loss:.2f} intra={p.intra:.3f} width={p.width}  |  "
                  f"h264: loss={f.loss:.2f} refresh={f.refresh} width={f.width}")
        return 0

    if args.input is None:
        build_parser().print_help()
        return 2
    if not args.input.exists():
        print(f"not found: {args.input}", file=sys.stderr)
        return 1

    try:
        # у движков разные ручки, поэтому и наборы настроек разные
        base = dataclasses.replace(
            resolve_ff(args.preset) if args.engine == "ffmpeg" else resolve(args.preset))
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2

    if args.engine == "ffmpeg" and not ff.available():
        print("ffmpeg not found in PATH", file=sys.stderr)
        return 1

    if args.width:
        base.width = args.width
    if args.out_width:
        base.out_width = args.out_width
    _apply_overrides(base, args.overrides)

    files = _inputs(args.input)
    if not files:
        print(f"no images in {args.input}", file=sys.stderr)
        return 1

    many = len(files) > 1 or args.variants > 1
    outdir: Path | None = None
    if many:
        outdir = args.output or (args.input if args.input.is_dir() else args.input.parent)
        outdir.mkdir(parents=True, exist_ok=True)

    engine = ff.degrade if args.engine == "ffmpeg" else degrade
    written = 0

    for path in files:
        try:
            img = Image.open(path)
            img.load()
        except Exception as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue

        for i in range(max(1, args.variants)):
            p = dataclasses.replace(base)
            p.seed = args.seed if args.seed is not None else None
            if p.seed is not None and args.variants > 1:
                p.seed += i

            out = engine(img, p)

            if many:
                suffix = f"_{i + 1}" if args.variants > 1 else ""
                dest = outdir / f"{path.stem}_jackal{suffix}.jpg"
            else:
                dest = args.output or path.with_name(f"{path.stem}_jackal.jpg")

            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.suffix.lower() in (".jpg", ".jpeg"):
                out.save(dest, quality=92, subsampling=1)
            else:
                out.save(dest)
            print(dest)
            written += 1

    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
