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
    import numpy as np
except ImportError:
    print("numpy is not installed in this environment. Run install.bat first.")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("Pillow is not installed in this environment. Run install.bat first.")
    sys.exit(1)


# Same parchment reference the live detector uses (detector.py
# DEFAULT_PARCHMENT_RGB) -- item cards and loot popups both render this
# same tan description-panel background, so excluding it here the same
# way the detector does keeps this tool's output consistent with what
# the app actually sees at runtime.
PARCHMENT_RGB = np.array([204, 172, 100])


def _rgb_to_hsv_np(rgb: np.ndarray):
    """Vectorized RGB (0-255) -> HSV (H in 0-360, S/V in 0-1). Matches
    colorsys.rgb_to_hsv semantics, just fast enough to run over every
    pixel in a full screenshot instead of one at a time."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.max(rgb, axis=-1)
    minc = np.min(rgb, axis=-1)
    v = maxc
    delta = maxc - minc
    safe_delta = np.where(delta == 0, 1, delta)
    safe_maxc = np.where(maxc == 0, 1, maxc)
    s = np.where(maxc == 0, 0, delta / safe_maxc)

    rc = (maxc - r) / safe_delta
    gc = (maxc - g) / safe_delta
    bc = (maxc - b) / safe_delta

    h = np.zeros_like(maxc, dtype=np.float64)
    h = np.where(maxc == r, bc - gc, h)
    h = np.where(maxc == g, 2.0 + rc - bc, h)
    h = np.where(maxc == b, 4.0 + gc - rc, h)
    h = (h / 6.0) % 1.0
    h = np.where(delta == 0, 0, h) * 360.0
    return h, s, v


def _analyze_title_text_color(img: Image.Image):
    """
    Isolates likely rarity TITLE TEXT pixels from an item-card or loot-
    popup screenshot, filtering out background/border art and the tan
    parchment panel -- the same categories of pixel the whole-image top-
    colors list above gets swamped by, since title text is a small
    fraction of total pixels. This mirrors detector.py's
    _sample_text_color(), which does the same background-exclusion trick
    on a small OCR box; here it runs over the whole image since there's
    no OCR box to anchor to.

    Reports the dominant hue cluster(s) among the surviving pixels so a
    rarity tier's real HSV center can be read directly off a real
    screenshot instead of guessed from the whole-image histogram (which
    is what repeatedly buried the real answer under background noise --
    see GitHub issue #5).
    """
    arr = np.array(img).astype(np.int16)
    pixels = arr.reshape(-1, 3)

    parchment_diff = np.abs(pixels - PARCHMENT_RGB).sum(axis=-1)
    # _rgb_to_hsv_np expects 0-1 inputs (matching colorsys convention) --
    # skipping this normalization was an earlier bug here: v came back on
    # a 0-255 scale while the threshold below assumed 0-100, so the
    # brightness filter silently never rejected anything.
    h, s, v = _rgb_to_hsv_np(pixels.astype(np.float64) / 255.0)

    # Item-card/loot-popup background art (the dark radial gradient behind
    # the item icon) is often a SIMILAR HUE to the rarity title text drawn
    # over it -- a green-tier card has both a dark green background and
    # bright green title text, for example -- so hue alone can't tell them
    # apart. What reliably does: brightness. A synthetic test confirmed a
    # looser v-threshold here (matching classify_rarity_from_rgb's noise
    # floor, meant for tiny anti-aliased OCR glyph edges) let a dark green
    # background get counted as "text" alongside the real bright green
    # title, corrupting the average. Real screenshots back this up too --
    # every background color in the sampled histograms topped out around
    # value ~20% (e.g. (52,36,40) at v~20%), while actual rarity title
    # text is a solid, vivid, much brighter color. Requiring high
    # saturation AND high value together also excludes the near-white/cream
    # text used for non-rarity lines (stat numbers, "Attack:", flavor
    # text), which is bright but low-saturation.
    text_mask = (s * 100 >= 30) & (v * 100 >= 45) & (parchment_diff >= 40)

    if text_mask.sum() < 10:
        print("\nNo likely title-text pixels found (image may be too small, "
              "or too far from the parchment/background reference colors "
              "this filter expects). Try a tighter crop around the title.")
        return

    text_pixels = pixels[text_mask]
    text_hues = h[text_mask]

    # Bucket by hue (10-degree bins) so anti-aliased edge pixels of the
    # same logical text color land in the same bucket instead of
    # fragmenting into many near-duplicate low-count entries.
    hue_bins = (text_hues // 10 * 10).astype(int)
    counts = Counter(hue_bins.tolist())
    total_text_pixels = len(text_pixels)

    text_sats = s[text_mask]
    text_vals = v[text_mask]

    print(f"\n{total_text_pixels} likely title-text pixels found "
          f"({100.0 * total_text_pixels / len(pixels):.2f}% of image)")
    print("Dominant text-color hue clusters (background/parchment excluded):\n")
    print("NOTE: a large cluster isn't automatically the right answer on a busy")
    print("full-card screenshot -- background art, borders, and anti-aliased")
    print("edges near the parchment panel can dominate by pixel COUNT without")
    print("being the actual solid title-text color. Avg Sat/Val columns help")
    print("tell them apart: solid rarity title text should read as a distinctly")
    print("HIGH, uniform saturation+value -- a cluster with mediocre/mixed")
    print("saturation despite a high pixel count is more likely a blend/halo,")
    print("not the real color. When in doubt, a tight crop of ONLY the title")
    print("word (cutting out background art, icon, borders, price) is far more")
    print("reliable than this whole-image analysis.\n")
    print(f"{'Hue bin':<10}{'Avg RGB':<18}{'HEX':<10}{'% of text px':<14}{'Avg Sat':<9}{'Avg Val':<9}")
    print("-" * 70)
    for hue_bin, count in counts.most_common(8):
        bucket_mask = hue_bins == hue_bin
        avg = text_pixels[bucket_mask].mean(axis=0)
        r, g, b = int(avg[0]), int(avg[1]), int(avg[2])
        pct = 100.0 * count / total_text_pixels
        avg_sat = text_sats[bucket_mask].mean() * 100
        avg_val = text_vals[bucket_mask].mean() * 100
        print(f"{hue_bin:>3}-{hue_bin+10:<5}({r:3d},{g:3d},{b:3d})   "
              f"#{r:02X}{g:02X}{b:02X}   {pct:5.2f}%       "
              f"{avg_sat:5.1f}%   {avg_val:5.1f}%")


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

    _analyze_title_text_color(img)


if __name__ == "__main__":
    main()
