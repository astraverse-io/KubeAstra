"""Turn rendered frames into the looping GIF.

    python assemble.py <frames-dir> <out.gif>

One shared 128-colour palette (taken from the busiest frames), no dithering so
terminal text stays crisp, and identical consecutive frames merged into longer
holds — 254 rendered frames come out as ~94, about 0.6 MB.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageChops


def main() -> None:
    frames_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    meta = json.loads((frames_dir / "meta.json").read_text())
    step = meta["step_ms"]
    frames = [Image.open(p).convert("RGB") for p in sorted(frames_dir.glob("*.png"))]
    w, h = frames[0].size

    # Palette source: the full terminal, the closing card, and swatches of the
    # window dots, which are too small to survive median-cut otherwise.
    ref = Image.new("RGB", (w, h * 2 + 60))
    ref.paste(frames[meta["full_terminal_frame"]], (0, 0))
    ref.paste(frames[-1], (0, h))
    for k, colour in enumerate(["#FF5F57", "#FEBC2E", "#28C840"]):
        ref.paste(Image.new("RGB", (w // 3, 60), colour), (k * (w // 3), 2 * h))
    palette = ref.quantize(colors=128, method=Image.Quantize.MEDIANCUT)

    kept, durations = [], []
    for f in frames:
        q = f.quantize(palette=palette, dither=Image.Dither.NONE)
        if kept and ImageChops.difference(q.convert("RGB"), kept[-1].convert("RGB")).getbbox() is None:
            durations[-1] += step
        else:
            kept.append(q)
            durations.append(step)
    durations[-1] += 1200  # rest on the closing card before the loop restarts

    out.parent.mkdir(parents=True, exist_ok=True)
    kept[0].save(out, save_all=True, append_images=kept[1:], duration=durations,
                 loop=0, optimize=True, disposal=1)
    print(f"assemble: {len(frames)} frames -> {len(kept)}, {sum(durations) / 1000:.1f}s, "
          f"{os.path.getsize(out) / 1024 / 1024:.2f} MB -> {out}")


if __name__ == "__main__":
    main()
