import io

import pytest
from PIL import Image, ImageChops

from hub_bot.resources import lobster_font, times_new_roman_font
from hub_bot.utils import caption_layout as layout

TEXTS = [
    "Длинная русская подпись с буквами Ё, й и щ: слова должны помещаться целиком. " * 5,
    "A long English caption with wide words, gjpq descenders and italic overhangs. " * 5,
    "W" * 180,
    '«Не бойся», — сказал он: "it\'s fine"; 100% [ready], \\path\\file.',
    "Первая строка\n\nВторая строка\nThird line",
    "Пятница 🙂 ✨ ❤️\nHappy friends 👨‍👩‍👧‍👦!",
]


def source(size=(640, 480), color="#527893", exif=None):
    file = io.BytesIO()
    image = Image.new("RGB", size, color)
    if exif:
        image.save(file, "JPEG", exif=exif)
    else:
        image.save(file, "PNG")
    file.seek(0)
    return file


@pytest.mark.parametrize("font", [lobster_font, times_new_roman_font])
@pytest.mark.parametrize("text", TEXTS)
def test_readable_caption_ink_stays_inside_its_rectangle(font, text):
    caption = layout.fit_caption(text, font, 580, 350, preferred_size=48, stroke=1)
    assert caption.font.size >= layout.MIN_FONT_SIZE
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


@pytest.mark.parametrize("text", ["x" * 1025, "Word " * 200])
def test_unreadable_or_oversized_captions_have_clear_length_policy(text):
    with pytest.raises(layout.CaptionLayoutError, match="подписи|помещается"):
        layout.fit_caption(text, lobster_font, 296, 150, 40)


def test_exif_orientation_is_applied_before_layout():
    exif = Image.Exif()
    exif[274] = 6
    assert layout.base_image(source((640, 480), exif=exif)).size == (480, 640)


def test_wide_short_image_does_not_fit_long_text_by_making_it_tiny():
    with pytest.raises(layout.CaptionLayoutError, match="помещается"):
        layout.lobster_image(source((1600, 120)), "A long caption that must stay readable. " * 20)


def test_lobster_preserves_transparent_pixels_outside_caption():
    file = io.BytesIO()
    Image.new("RGBA", (640, 480), (10, 20, 30, 0)).save(file, "PNG")
    file.seek(0)
    image = layout.lobster_image(file, "Привет")
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0)) == (10, 20, 30, 0)
    assert image.getchannel("A").getbbox() is not None
