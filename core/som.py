"""Set-of-Mark (SoM) visual prompting helpers for Android and iOS screen captures.

Draws numbered bounding boxes onto a `.som.png` sibling image and records the
element -> coordinate mapping in `<nn>.som.json` and `latest_som.json` so a VLM
can cite `[element_id]` and call `tap_element` instead of guessing raw pixels.
"""

import json
import shutil
from pathlib import Path

try:
    from PIL import ImageDraw, ImageFont
except ImportError:  # pragma: no cover - Pillow is optional at import time
    ImageDraw = None
    ImageFont = None

PALETTE = [
    (220, 38, 38),
    (37, 99, 235),
    (5, 150, 105),
    (217, 119, 6),
    (124, 58, 237),
    (219, 39, 119),
]


def _clamp_box(bounds, width, height, draw_scale):
    x1, y1, x2, y2 = bounds
    sx1 = max(0, min(width - 1, int(round(x1 * draw_scale))))
    sy1 = max(0, min(height - 1, int(round(y1 * draw_scale))))
    sx2 = max(sx1 + 1, min(width - 1, int(round(x2 * draw_scale))))
    sy2 = max(sy1 + 1, min(height - 1, int(round(y2 * draw_scale))))
    return sx1, sy1, sx2, sy2


def annotate_and_save(out_dir, nn, img, elements, platform, draw_scale=1.0):
    """Draw numbered Set-of-Mark boxes onto `img` and persist `.som.png` / `.som.json`.

    Returns ``(som_png_path, som_json_path)``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    som_png = out / f"{nn}.som.png"
    som_json = out / f"{nn}.som.json"
    latest_json = out / "latest_som.json"

    if isinstance(img, (str, Path)):
        raw_src = Path(img)
        if raw_src.is_file() and raw_src.resolve() != som_png.resolve():
            shutil.copyfile(raw_src, som_png)
    elif hasattr(img, "convert") and ImageDraw is not None:
        annotated = img.convert("RGB")
        draw = ImageDraw.Draw(annotated)
        font = ImageFont.load_default() if ImageFont is not None else None
        w, h = annotated.size
        for pos, el in enumerate(elements):
            idx = el["index"]
            color = PALETTE[pos % len(PALETTE)]
            x1, y1, x2, y2 = _clamp_box(el["bounds"], w, h, draw_scale)
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
            tag = str(idx)
            tag_w = max(14, 7 * len(tag) + 6)
            tag_h = 14
            tx1 = x1
            ty1 = max(0, y1 - tag_h) if y1 >= tag_h else y1
            tx2 = min(w - 1, tx1 + tag_w)
            ty2 = min(h - 1, ty1 + tag_h)
            draw.rectangle([tx1, ty1, tx2, ty2], fill=color)
            draw.text((tx1 + 3, ty1 + 2), tag, fill=(255, 255, 255), font=font)
        annotated.save(som_png)
    elif hasattr(img, "save"):
        img.save(som_png)
    elif (out / f"{nn}.png").is_file():
        shutil.copyfile(out / f"{nn}.png", som_png)

    payload = {
        "screen": nn,
        "platform": platform,
        "image": str(som_png),
        "raw_image": str(out / f"{nn}.png"),
        "elements": {str(el["index"]): el for el in elements},
    }
    encoded = json.dumps(payload, indent=2)
    som_json.write_text(encoded, encoding="utf-8")
    latest_json.write_text(encoded, encoding="utf-8")
    return som_png, som_json


def _candidate_som_files(root, directory=""):
    base = Path(root).resolve()
    if directory:
        target = (base / directory).resolve()
        if str(target).startswith(str(base)):
            direct = target / "latest_som.json"
            if direct.is_file():
                return [direct]
            nested = sorted(target.rglob("latest_som.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if nested:
                return nested
    candidates = []
    for search_root in (base / "results", base):
        if (search_root / "latest_som.json").is_file():
            candidates.append(search_root / "latest_som.json")
        if search_root.is_dir():
            candidates.extend(search_root.rglob("latest_som.json"))
    unique = list({p.resolve(): p for p in candidates if p.is_file()}.values())
    unique.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return unique


def resolve_element(root, element_id, directory=""):
    """Look up `element_id` in the most recent `latest_som.json` under `root`.

    Returns ``{"element": ..., "tap_x": int, "tap_y": int, "platform": str, "som_path": str}``
    or ``None`` if not found.
    """
    key = str(element_id).strip()
    if not key:
        return None
    for som_file in _candidate_som_files(root, directory):
        try:
            data = json.loads(som_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        el = (data.get("elements") or {}).get(key)
        if isinstance(el, dict) and "cx" in el and "cy" in el:
            return {
                "element": el,
                "tap_x": int(el["cx"]),
                "tap_y": int(el["cy"]),
                "platform": str(data.get("platform") or "android"),
                "som_path": str(som_file),
            }
    return None
