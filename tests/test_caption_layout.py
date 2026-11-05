import io

import pytest
from PIL import Image, ImageChops, ImageFont

from msu_hub_bot.resources import lobster_font, meme_font, times_new_roman_font
from msu_hub_bot.media import caption_layout as layout
from msu_hub_bot.media import limits

TEXTS = [
    "Длинная русская подпись с буквами Ё, й и щ: слова должны помещаться целиком. " * 5,
    "A long English caption with wide words, gjpq descenders and italic overhangs. " * 5,
    "W" * 180,
    '«Не бойся», — сказал он: "it\'s fine"; 100% [ready], \\path\\file.',
    "Первая строка\n\nВторая строка\nThird line",
    "Пятница 🙂 ✨ ❤️\nHappy friends 👨‍👩‍👧‍👦!",
]


def test_oversized_caption_source_is_rejected_before_decoding(monkeypatch):
    file = source((2, 2))
    monkeypatch.setattr(limits, "MAX_IMAGE_PIXELS", 3)

    def decode(*args, **kwargs):
        pytest.fail("caption source decoded before checking dimensions")

    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(limits.MediaDimensionsError):
        layout.base_image(file)
    assert not file.closed


def source(size=(640, 480), color="#527893", exif=None):
    file = io.BytesIO()
    image = Image.new("RGB", size, color)
    if exif:
        image.save(file, "JPEG", exif=exif)
    else:
        image.save(file, "PNG")
    file.seek(0)
    return file


@pytest.mark.parametrize("font", [lobster_font, times_new_roman_font, meme_font])
@pytest.mark.parametrize("text", TEXTS)
def test_readable_caption_ink_stays_inside_its_rectangle(font, text):
    caption = layout.fit_caption(text, font, 580, 350, preferred_size=48, stroke=1)
    image = Image.new("RGBA", (640, 410), (0, 0, 0, 0))
    caption.draw(image, 30, 30)
    left, top, right, bottom = image.getchannel("A").getbbox()
    assert 30 <= left < right <= 610
    assert 30 <= top < bottom <= 380
    assert caption.text.replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


@pytest.mark.parametrize("size", [(640, 480), (320, 960), (1600, 240), (1, 2048), (2048, 1), (2, 3)])
def test_lobster_extreme_images_have_margin_below_complete_caption(size):
    file = source(size)
    before = layout.base_image(file)
    file.seek(0)
    after = layout.lobster_image(file, "Друзья\nHello gjpq")
    assert after.size == before.size
    bbox = ImageChops.difference(before, after.convert("RGB")).getbbox()
    assert bbox is not None
    assert 0 < bbox[0] < bbox[2] < after.width
    assert 0 < bbox[1] < bbox[3] < after.height
    assert after.width <= layout.MAX_IMAGE_SIDE and after.height <= layout.MAX_IMAGE_SIDE


@pytest.mark.parametrize("text", TEXTS)
def test_demotivator_caption_has_black_padding_on_every_side(text):
    panel = layout.demotivator_caption(744, text, 46)
    left, top, right, bottom = panel.getbbox()
    assert 46 <= left < right <= panel.width - 46
    assert 46 <= top < bottom <= panel.height - 46
    assert panel.height % 2 == 0


def test_long_token_is_split_without_losing_characters():
    text = "Supercalifragilisticexpialidocious" * 5
    caption = layout.fit_caption(text, lobster_font, 300, 900, 40)
    assert "\n" in caption.text
    assert caption.text.replace("\n", "") == text


@pytest.mark.parametrize("text", ["x" * 4096, "0123456789abcdef" * 256])
def test_long_token_wrapping_bounds_total_font_measurement_work(monkeypatch, text):
    font = ImageFont.truetype(str(lobster_font), 40)
    original = font.getbbox
    measured_characters = 0

    def measured(value, *args, **kwargs):
        nonlocal measured_characters
        measured_characters += len(value)
        # Shaping is proportional to input size; a call count misses costly
        # repeated measurements of the entire unwrapped suffix.
        assert measured_characters <= len(text) * 16
        return original(value, *args, **kwargs)

    monkeypatch.setattr(font, "getbbox", measured)
    wrapped = layout._wrap(text, font, 296, 0)
    assert wrapped.replace("\n", "") == text
    assert all(original(line)[2] - original(line)[0] <= 296 for line in wrapped.splitlines())


@pytest.mark.parametrize("cluster", ["a\u0301", "❤️", "👍🏽", "👨‍👩‍👧‍👦"])
def test_caption_wrapping_keeps_combining_and_joined_sequences_together(cluster):
    font = ImageFont.truetype(str(meme_font), 20)
    left, _, right, _ = font.getbbox(cluster)
    wrapped = layout._wrap(cluster * 6, font, right - left + 1, 0)
    assert wrapped.replace("\n", "") == cluster * 6
    assert all(line == cluster * (len(line) // len(cluster)) for line in wrapped.splitlines())


@pytest.mark.parametrize("cluster", ["a\u0301", "❤️", "👍🏽", "👨‍👩‍👧‍👦"])
def test_oversized_unicode_cluster_is_shrunk_without_splitting_it(cluster):
    caption = layout.fit_caption(cluster, meme_font, 1, 30, 40)
    assert caption.text == cluster
    assert caption.width == 1
    assert 0 < caption.height <= 30


@pytest.mark.parametrize("font", [lobster_font, times_new_roman_font, meme_font])
@pytest.mark.parametrize("text", ["x" * 4096, "Word " * 1500, "Строка\n" * 580])
def test_long_captions_shrink_without_losing_text_or_exceeding_canvas(font, text):
    caption = layout.fit_caption(text, font, 296, 150, 40)
    assert 0 < caption.width <= 296
    assert 0 < caption.height <= 150
    assert caption.font.size < 40
    assert "".join(caption.text.split()) == "".join(text.split())
    with Image.new("RGBA", (316, 170)) as image:
        caption.draw(image, 10, 10)
        left, top, right, bottom = image.getchannel("A").getbbox()
        assert 10 <= left < right <= 306
        assert 10 <= top < bottom <= 160


def test_exif_orientation_is_applied_before_layout():
    exif = Image.Exif()
    exif[274] = 6
    assert layout.base_image(source((640, 480), exif=exif)).size == (480, 640)


def test_wide_short_image_fits_long_text_by_shrinking_it():
    file = source((1600, 120))
    before = layout.base_image(file, preserve_alpha=True)
    after = layout.lobster_image(file, "A long caption that fits in full. " * 125)
    assert after.size == before.size
    bounds = ImageChops.difference(before, after).convert("RGB").getbbox()
    assert bounds is not None
    assert 0 < bounds[0] < bounds[2] < after.width
    assert 0 < bounds[1] < bounds[3] < after.height


def test_lobster_preserves_transparent_pixels_outside_caption():
    file = io.BytesIO()
    Image.new("RGBA", (640, 480), (10, 20, 30, 0)).save(file, "PNG")
    file.seek(0)
    image = layout.lobster_image(file, "Привет")
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0)) == (10, 20, 30, 0)
    assert image.getchannel("A").getbbox() is not None


@pytest.mark.parametrize("text", ["мем", "Ёжик gjpq", "Первая строка\nВторая строка", TEXTS[0] * 8])
def test_demotivator_centers_visible_ink_below_the_white_frame(text):
    with source() as file, layout.demotivator_image(file, text) as image:
        border = layout.frame_border(640)
        # The bottom panel starts immediately below the outside white frame.
        panel_top = border + 480 + 12
        with image.crop((0, panel_top, image.width, image.height)) as panel:
            left, top, right, bottom = panel.getbbox()
            assert abs(top - (panel.height - bottom)) <= 1
            assert abs(left - (panel.width - right)) <= 1
            assert top >= border and panel.height - bottom >= border
        assert image.getpixel((border, panel_top - 1)) == (255, 255, 255)


@pytest.mark.parametrize("text", ["Привет, друзья!", TEXTS[0], TEXTS[1] * 8])
def test_meme_has_centered_black_ink_above_unmodified_media(text):
    with source() as file, layout.meme_image(file, text) as image:
        panel_height = image.height - 480
        with image.crop((0, 0, image.width, panel_height)) as panel:
            with Image.new("RGB", panel.size, "white") as white:
                bounds = ImageChops.difference(white, panel).getbbox()
            assert bounds is not None
            left, top, right, bottom = bounds
            assert abs(top - (panel.height - bottom)) <= 1
            assert abs(left - (panel.width - right)) <= 1
            assert top >= 12 and left >= 12
        with image.crop((0, panel_height, image.width, image.height)) as media:
            assert media.getextrema() == ((82, 82), (120, 120), (147, 147))


@pytest.mark.parametrize("style", ["lobster", "demotivator", "meme"])
def test_image_and_video_frame_share_geometry_and_owned_overlay(style):
    frame = layout.caption_frame((640, 480), "Общий кадр", style)
    with frame.overlay:
        assert frame.overlay.mode == "RGBA"
        assert frame.overlay.width % 2 == frame.overlay.height % 2 == 0
        with source() as file, layout.caption_image(file, "Общий кадр", style) as image:
            assert image.size == frame.overlay.size
        x, y = frame.media_position
        assert frame.overlay.getpixel((x, y)) == (0, 0, 0, 0)


def test_empty_caption_remains_an_actionable_error():
    with pytest.raises(layout.CaptionLayoutError, match="Добавь текст"):
        layout.fit_caption(" \n\t", meme_font, 100, 100, 20)


@pytest.mark.parametrize("style", ["lobster", "demotivator", "meme"])
def test_caption_emoji_render_in_colour_instead_of_missing_glyph_boxes(style):
    source = io.BytesIO()
    Image.new("RGB", (480, 320), (128, 128, 128)).save(source, format="PNG")
    plain = layout.caption_image(io.BytesIO(source.getvalue()), "Пятница", style).convert("RGB")
    with_emoji = layout.caption_image(io.BytesIO(source.getvalue()), "Пятница 🔥", style).convert("RGB")
    # The source, caption text and panels are greyscale; only emoji bring saturated pixels.
    saturated = [max(pixel) - min(pixel) > 60 for pixel in with_emoji.getdata()]
    assert sum(saturated) > 100
    assert not any(max(pixel) - min(pixel) > 60 for pixel in plain.getdata())


def test_text_without_emoji_keeps_pillow_multiline_layout():
    assert not layout.fit_caption("Пятница", meme_font, 300, 200, 40).rich
    caption = layout.fit_caption("Пятница 🙂\nвторая строка", meme_font, 300, 200, 40)
    assert caption.rich and caption.text.replace("\n", " ").split() == ["Пятница", "🙂", "вторая", "строка"]
    assert caption.width <= 300 and caption.height <= 200
