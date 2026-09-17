"""Measured captions for image overlays and framed image/video demotivators."""

import re
import math
from dataclasses import dataclass
from pathlib import Path
from io import BytesIO
from typing import cast

from PIL import Image, ImageDraw, ImageFont, ImageOps

from msu_hub_bot.resources import lobster_font, times_new_roman_font

MAX_CAPTION_LENGTH = 1024
MIN_FONT_SIZE = 18
MAX_IMAGE_SIDE = 1600


class CaptionLayoutError(ValueError):
    pass


@dataclass
class Caption:
    text: str
    font: ImageFont.FreeTypeFont
    bounds: tuple[int, int, int, int]
    spacing: int
    stroke: int = 0

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    def draw(self, image: Image.Image, left: int, top: int) -> None:
        ImageDraw.Draw(image).multiline_text(
            (left - self.bounds[0], top - self.bounds[1]),
            self.text,
            font=self.font,
            spacing=self.spacing,
            align="center",
            fill="white",
            stroke_width=self.stroke,
            stroke_fill="black",
        )


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, stroke: int) -> str:
    def fits(value: str) -> bool:
        left, _, right, _ = font.getbbox(value, stroke_width=stroke)
        return right - left <= width

    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if line and fits(line + " " + word):
                line += " " + word
                continue
            if line:
                lines.append(line)
                line = ""
            while not fits(word):
                low, high = 1, len(word)
                while low < high:
                    middle = (low + high + 1) // 2
                    if fits(word[:middle]):
                        low = middle
                    else:
                        high = middle - 1
                if not fits(word[:low]):
                    raise CaptionLayoutError("Подпись не помещается. Возьми картинку побольше.")
                lines.append(word[:low])
                word = word[low:]
            line = word
        lines.append(line)
    return "\n".join(lines)


def fit_caption(text: str, font_path: Path, width: int, height: int, preferred_size: int, stroke: int = 0) -> Caption:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_CAPTION_LENGTH:
        raise CaptionLayoutError(f"В подписи максимум {MAX_CAPTION_LENGTH} символа. Сократи текст.")
    # Preserve paragraph breaks without spending the whole canvas on blank rows.
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    if not text:
        raise CaptionLayoutError("Добавь текст для подписи.")
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))

    def layout(size: int) -> Caption:
        font = ImageFont.truetype(str(font_path), size)
        spacing = max(3, size // 5)
        wrapped = _wrap(text, font, width, stroke)
        bounds = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing, align="center", stroke_width=stroke)
        pixel_bounds = (math.floor(bounds[0]), math.floor(bounds[1]), math.ceil(bounds[2]), math.ceil(bounds[3]))
        return Caption(wrapped, font, pixel_bounds, spacing, stroke)

    # Wide images are downscaled in chat; keep their smallest text proportional.
    minimum_size = max(MIN_FONT_SIZE, round(width / 32))
    low, high = minimum_size, max(minimum_size, preferred_size)
    preferred = layout(high)
    if preferred.width <= width and preferred.height <= height:
        return preferred
    high -= 1
    best = None
    while low <= high:
        size = (low + high) // 2
        caption = layout(size)
        if caption.width <= width and caption.height <= height:
            best = caption
            low = size + 1
        else:
            high = size - 1
    if best is None:
        raise CaptionLayoutError("Подпись не помещается целиком. Сократи текст или возьми картинку побольше.")
    return best


def base_image(file: BytesIO, preserve_alpha: bool = False) -> Image.Image:
    with Image.open(file) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    if max(image.size) < 320:
        scale = 320 / max(image.size)
        image = image.resize(cast(tuple[int, int], tuple(max(1, round(side * scale)) for side in image.size)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA" if preserve_alpha else "RGB", (max(320, image.width), max(240, image.height)), "black")
    canvas.paste(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2), None if preserve_alpha else image)
    return canvas


def lobster_image(file: BytesIO, text: str) -> Image.Image:
    image = base_image(file, preserve_alpha=True)
    margin = max(12, round(image.width * 0.04))
    bottom = max(12, round(image.height * 0.08))
    caption = fit_caption(
        text,
        lobster_font,
        image.width - margin * 2,
        image.height - bottom - max(12, image.height // 5),
        preferred_size=min(100, round(0.0669 * image.width + 4.2772)),
        stroke=1,
    )
    caption.draw(image, (image.width - caption.width) // 2, image.height - bottom - caption.height)
    return image


def frame_border(width: int) -> int:
    return max(round((width + 12) * 0.07), 20)


def demotivator_caption(width: int, text: str, border: int) -> Image.Image:
    caption = fit_caption(
        text,
        times_new_roman_font,
        width - border * 2,
        min(1200, max(240, width)),
        preferred_size=min(96, round(width * 0.075)),
    )
    height = caption.height + border * 2
    height += height % 2  # Video encoders require even frame dimensions.
    panel = Image.new("RGB", (width, height), "black")
    caption.draw(panel, (width - caption.width) // 2, border)
    return panel


def demotivator_image(file: BytesIO, text: str) -> Image.Image:
    source = base_image(file)
    border = frame_border(source.width)
    framed = ImageOps.expand(ImageOps.expand(source, border=3, fill="black"), border=3, fill="white")
    framed = ImageOps.expand(framed, border=border, fill="black")
    caption = demotivator_caption(framed.width, text, border)
    result = Image.new("RGB", (framed.width, framed.height + caption.height), "black")
    result.paste(framed, (0, 0))
    result.paste(caption, (0, framed.height))
    return result
