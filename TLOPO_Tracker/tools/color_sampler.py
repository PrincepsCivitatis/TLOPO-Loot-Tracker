"""
color_sampler.py
Standalone debug helper -- NOT part of the main app.

Reads a screenshot image file and reports the most common colors in it,
so we can read exact RGB values off a real loot popup screenshot instead
of guessing from a description.

Usage (from the TLOPO_Tracker folder, with the venv active):
    venv\\Scripts\\python tools\\color_sampler.py path\\to\\screenshot.png

Or just double-click run_color_sampler.bat and drag a screenshot onto it.
"""

import sys
from collections import Counter

try:
    from PIL import Image
except ImportError:
    print("Pillow is not installed in this environment. Run install.bat first.")
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: color_sampler.py <path-to-screenshot.png>")
        sys.exit(1)

    path = sys.argv[1]
    img = Image.open(path).convert("RGB")

    # Downsample for speed on large screenshots -- color distribution is
    # still representative at a lower resolution.
    w, h = img.size
    max_dim = 900
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)))

    pixels = list(img.getdata())
    # Quantize slightly (round to nearest 4) so near-identical anti-aliased
    # shades group together into one meaningful bucket.
    quantized = [(r // 4 * 4, g // 4 * 4, b // 4 * 4) for (r, g, b) in pixels]

    counts = Counter(quantized)
    total = len(quantized)

    print(f"Image size analyzed: {img.size[0]}x{img.size[1]} ({total} pixels)")
    print(f"Top 25 most common colors:\n")
    print(f"{'RGB':<18}{'HEX':<10}{'% of image':<12}")
    print("-" * 40)
    for (r, g, b), count in counts.most_common(25):
        pct = 100.0 * count / total
        hexcode = f"#{r:02X}{g:02X}{b:02X}"
        print(f"({r:3d},{g:3d},{b:3d})   {hexcode:<10}{pct:5.2f}%")


if __name__ == "__main__":
    main()
