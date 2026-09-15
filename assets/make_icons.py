"""Build every icon size Beehive Wire serves, from the one source artwork.

Source: assets/source-icon.png (the plate with the hive, the bee and the wordmark).

Two treatments come out of it:
  * the full plate, flattened edge to edge  -> home-screen and manifest icons
  * the hive-and-bee alone, wordmark cropped off -> favicon and the install bar,
    because at 32px a wordmark is just grey mush
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "source-icon.png"
OUT = ROOT / "static"
PLATE = (17, 22, 27)          # the artwork's own background
DIFF = 90                     # how far off PLATE a pixel must be to count as ink


def content_box(im: Image.Image, top: int, bottom: int) -> tuple[int, int, int, int]:
    """Tight box around the non-background pixels between two rows."""
    px = im.load()
    W, _ = im.size
    x0, x1 = W, 0
    for y in range(top, bottom + 1):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a > 200 and abs(r - PLATE[0]) + abs(g - PLATE[1]) + abs(b - PLATE[2]) > DIFF:
                if x < x0: x0 = x
                if x > x1: x1 = x
    return x0, top, x1, bottom


def bands(im: Image.Image) -> list[tuple[int, int]]:
    """Rows of the plate that hold ink, as (first, last) pairs."""
    px = im.load()
    W, H = im.size
    out, run = [], None
    for y in range(H):
        hit = 0
        for x in range(0, W, 2):
            r, g, b, a = px[x, y]
            if a > 200 and abs(r - PLATE[0]) + abs(g - PLATE[1]) + abs(b - PLATE[2]) > DIFF:
                hit += 1
                if hit > 2:
                    break
        on = hit > 2
        if on and run is None:
            run = y
        elif not on and run is not None:
            out.append((run, y - 1))
            run = None
    if run is not None:
        out.append((run, H - 1))
    return out


def plate(im: Image.Image, margin: float = 0.0, side: int | None = None) -> Image.Image:
    """Centre an image on an opaque PLATE-coloured square."""
    side = side or int(max(im.size) * (1 + 2 * margin))
    out = Image.new("RGBA", (side, side), PLATE + (255,))
    out.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC).convert("RGBA")
    side = max(src.size)

    full = plate(src, side=side)                       # edge to edge, no transparency

    shrunk = src.resize((int(side * 0.78),) * 2, Image.LANCZOS)
    maskable = plate(shrunk, side=side)                # inset, so Android's circle can't clip it

    ink = bands(src)
    top, bottom = ink[0]                               # hive + bee; the wordmark is the band under it
    x0, _, x1, _ = content_box(src, top, bottom)
    mark = plate(src.crop((x0, top, x1, bottom)), margin=0.12)

    jobs = [
        (full, "icon-192.png", 192), (full, "icon-512.png", 512),
        (maskable, "icon-maskable-512.png", 512),
        (full, "apple-touch-icon.png", 180),
        (mark, "icon-mark-192.png", 192), (mark, "favicon-32.png", 32),
    ]
    for img, name, px in jobs:
        img.resize((px, px), Image.LANCZOS).convert("RGB").save(OUT / name, optimize=True)
        print(f"  {name}  {px}x{px}")


if __name__ == "__main__":
    main()
