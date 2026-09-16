import gzip
import random
from io import BytesIO
from typing import Optional

import lottie
from aiogram.types import Message
from aiogram.utils.markdown import hcode
from fontTools.pens.boundsPen import BoundsPen
from lottie import NVector, objects
from lottie.objects import easing
from lottie.utils import script
from lottie.utils.color import Color
from lottie.utils.font import Font, GlyphMetrics, RawFontRenderer

from common.executor import TPExecutor
from common import json
from common.tg.filters import MetaInfo
from common.tg.files import input_file
from common.utils import bytes_io
from hub_bot.resources import ubuntu_mono_font


class OutlineFont(Font):  # type: ignore[misc]  # The upstream font adapter has no typing metadata.
    """Read glyph bounds through fontTools' public outline/pen protocol."""

    def glyph(self, glyph_name: str) -> GlyphMetrics:
        glyph = self.glyphset[glyph_name]
        bounds = BoundsPen(self.glyphset)
        glyph.draw(bounds)
        xmin, _, xmax, _ = bounds.bounds or (glyph.lsb, 0, glyph.width, 0)
        return GlyphMetrics(glyph, glyph.lsb, glyph.width, xmin, xmax)


class OutlineFontRenderer(RawFontRenderer):  # type: ignore[misc]  # The upstream renderer has no typing metadata.
    def __init__(self, filename: str) -> None:
        super().__init__(filename)
        self._font = OutlineFont(self.font.wrapped)


ubuntu_mono_renderer = OutlineFontRenderer(str(ubuntu_mono_font))


def shift(s: str, k: int) -> str:
    k %= len(s)
    return s[-k:] + s[:-k]


class MatrixSticker:
    """
    Author: https://gitlab.com/mattia.basaglia/sticker_scripts
    """

    def __init__(self, text: str) -> None:
        self.last_frame = 180
        self.animation = objects.Animation(self.last_frame)
        self.offset_time = 5
        self.font = ubuntu_mono_renderer
        self.font_size = 25.5
        self.ex = self.font.ex(self.font_size) + 3
        self.line_height = self.font.line_height(self.font_size)
        self.loop_time = 120
        self.n_lines = 0
        self.n_rows = 6 * 4

        text += " "
        self.columns = [shift(text, k)[: self.n_rows] for k in range(min(6, len(text)))]

    def character(self, ch: str, parent: objects.ShapeLayer, time: int, y_off: int) -> None:
        group = parent.add_shape(self.font.render(ch, self.font_size).shapes[0])
        group.transform.position.value.y += self.line_height * y_off
        fill = group.add_shape(objects.Fill())
        color = Color(0, 1, 0)
        fill.color.add_keyframe(time + 0, color)
        fill.color.add_keyframe(time + 40, Color(0, 0, 0), easing.Jump())
        if time != 0:
            fill.opacity.add_keyframe(0, 0, easing.Jump())
            fill.opacity.add_keyframe(time, 100, easing.Jump())
        fill.opacity.add_keyframe(time + 20, 100)
        fill.opacity.add_keyframe(time + 80, 0)
        group.add_shape(objects.Stroke(Color(0, 0, 0), 2)).opacity = fill.opacity

    def add_rain(self, layer_id: str, x: int, y: int, parent: objects.Precomp) -> None:
        layer = objects.PreCompLayer(layer_id)
        parent.add_layer(layer)
        layer.transform.position.value.x = self.ex * x
        layer.transform.position.value.y = self.line_height * y * 4
        start_time = y * self.offset_time * 4

        layer.start_time = start_time

    def make_line(self, s: str) -> None:
        layer_id = f"line{self.n_lines}"
        self.n_lines += 1
        pc = objects.Precomp(layer_id, self.animation)
        self.animation.assets.append(pc)

        layer = pc.add_layer(objects.ShapeLayer())
        for i, c in enumerate(s):
            self.character(c, layer, i * self.offset_time, i + 1)

    def add_line(self, layer_id: str, x: int, off: float) -> None:
        pcl = objects.PreCompLayer(layer_id)
        self.animation.add_layer(pcl)
        pcl.transform.position.value.x = x * self.ex
        start_time = self.last_frame * off
        pcl.start_time = start_time

    def generate(self) -> objects.Animation:
        for column in self.columns:
            line = column
            while len(line) < self.n_rows:
                line += column
            self.make_line(line[: self.n_rows])

        n_cols = 32
        n_offsets = 16
        off: list[int] = []
        for i in range(0, n_cols, n_offsets):
            t_off = list(range(n_offsets))
            random.shuffle(t_off)
            while off and off[-1] in t_off[:3]:
                random.shuffle(t_off)
            off += t_off

        for i in range(n_cols):
            norm_off = off[i] / n_offsets
            line_id = f"line{random.randint(0, self.n_lines - 1)}"
            self.add_line(line_id, i, norm_off)
            self.add_line(line_id, i, norm_off - 1)

        script.float_strip(self.animation)
        return self.animation


color_pairs = (
    (Color(0, 0, 0), Color(0.98, 0.59, 0.12)),  # hub
    (Color(0.95, 0.98, 0.93), Color(0.90, 0.22, 0.27)),  # молочно красный
    (Color(0.95, 0.98, 0.93), Color(0.27, 0.48, 0.62)),  # молочно голубой
)


class AnimateTextSticker:
    def __init__(self, text: str) -> None:
        self.text = text[:11] + " "

    def generate(self) -> objects.Animation | None:
        x_scale = -1
        last_frame = 40
        animation = lottie.objects.Animation(last_frame)

        marker = lottie.objects.BoundingBox(0, 200, 512, 400)
        font_size = marker.height
        font = ubuntu_mono_renderer

        fill_color, stroke_color = random.choice(color_pairs)
        stroke_width = 12

        text_layer = animation.add_layer(lottie.objects.ShapeLayer())
        container = lottie.objects.Group()
        pos = NVector(0, 0)
        g = font.render(self.text, font_size, pos)
        text_size = NVector(pos.x, g.bounding_box().height)
        if not (text_size.x > 0 and text_size.y > 0):
            return None
        scale = min(marker.width / text_size.x, marker.height / text_size.y)
        g.transform.scale.value *= scale
        g.transform.position.value.x = g.transform.anchor_point.value.x = text_size.x / 2
        g.transform.scale.value.x *= x_scale
        text_size *= scale
        dy = (marker.height - text_size.y) / 2 + text_size.y
        container.add_shape(g)

        repeater = container.add_shape(lottie.objects.Repeater())
        repeater.copies.value = 2
        repeater.transform.position.value.x = text_size.x  # marker.width
        container.add_shape(lottie.objects.Fill(fill_color))
        container.add_shape(lottie.objects.Stroke(stroke_color, stroke_width))

        conpos = NVector(marker.x1, marker.y1 + dy)
        container.transform.position.add_keyframe(0, conpos + NVector(-text_size.x, 0))
        container.transform.position.add_keyframe(last_frame, conpos + NVector(0, 0))
        text_layer.add_shape(container)

        if x_scale == -1:
            for layer in animation.layers:
                sh = layer.shapes.pop(0)
                g = layer.insert_shape(0, lottie.objects.Group())
                g.add_shape(sh)
                g.transform.scale.value.x *= x_scale
                g.transform.position.value = g.transform.anchor_point.value = NVector(256, 256)

        return animation


def animate(builder: type[AnimateTextSticker] | type[MatrixSticker], text: str) -> Optional[BytesIO]:
    res = builder(text).generate()
    if not res:
        return None

    sticker = bytes(json.dumps(res.to_dict()), encoding="utf-8")
    return bytes_io(gzip.compress(sticker), "sticker.tgs")


async def reply_sticker(
    message: Message, meta: MetaInfo, builder: type[AnimateTextSticker] | type[MatrixSticker], cpu_executor: TPExecutor
) -> Message | bool:
    target, text = meta.extract_text()
    if not text:
        return True

    sticker, timeouted = await cpu_executor.run(animate, builder, text)
    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not sticker:
        return True
    return await target.reply_sticker(input_file(sticker, "sticker.tgs"))


async def process_animate(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    return await reply_sticker(message, meta, AnimateTextSticker, cpu_executor)


async def process_matrix(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    return await reply_sticker(message, meta, MatrixSticker, cpu_executor)
