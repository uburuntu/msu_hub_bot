import io

from PIL import Image, ImageFont, ImageDraw, ImageOps
from aiogram.types import Message, ContentType, ChatType

from app import cpu_executor
from common.tg.chat_actioner import ChatActioner
from common.tg.filters import MetaInfo
from common.tg.middlewares.haiku import rate_keyboard
from common.tg.utils import action_by_type
from common.utils import image_bytes_io, cut_long_text_yield, megabytes, FakeBytesIO
from resources import lobster_font, times_new_roman_font
from utils.ffmpeg import ffmpeg


async def process_lobster(message: Message, meta: MetaInfo):
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    _, text = meta.extract_text()
    if not text:
        return True

    def approx_font_size(width: int) -> int:
        return int(0.0669 * width + 4.2772)

    async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
        image = Image.open(file).convert('RGBA')
        draw = ImageDraw.Draw(image)
        xy = int(image.width * 0.50), int(image.height * 0.85)
        text = '\n'.join(cut_long_text_yield(text, hard_max_len=21))[:240]
        font = ImageFont.truetype(str(lobster_font), max(10, min(100, approx_font_size(image.width))))
        draw.text(xy, text, fill='white', stroke_width=1, stroke_fill='black', align='center', anchor='ms', font=font)

    return await target.reply_photo(image_bytes_io(image, ext='png'),
                                    reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None)


def d_border(image, border=0, bottom=0):
    """
    Add black border to the image
    """
    left = top = right = border
    width = left + image.size[0] + right
    height = top + image.size[1] + bottom
    out = Image.new(image.mode, (width, height), 'black')
    out.paste(image, (left, top))
    return out


async def process_demotivator(message: Message, meta: MetaInfo):
    target, file = await meta.extract_video()
    if file:
        return await process_demotivator_video(message, meta)

    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    _, text = meta.extract_text()
    if not text:
        return True

    text = '\n'.join(cut_long_text_yield(text, hard_max_len=21))[:1024].replace('\n\n', '\n')

    def approx_font_size(width: int) -> int:
        return int(0.09 * width + 6)

    async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
        orig = ImageOps.expand(Image.open(file), border=3, fill='black')
        orig = o = ImageOps.expand(orig, border=3, fill='white')
        border = max(int(orig.width * 0.07), 20)
        orig = ImageOps.expand(orig, border=border, fill='black')

        font = ImageFont.truetype(str(times_new_roman_font), max(10, min(200, approx_font_size(o.width))))
        # Pillow 10 removed getsize_multiline; the bottom coordinate includes ascender space.
        _, _, _, h = ImageDraw.Draw(orig).multiline_textbbox((0, 0), text, font=font)
        image = d_border(orig, bottom=h + border)

        draw = ImageDraw.Draw(image)
        x = image.width // 2
        y = orig.height - border // 2
        draw.text((x, y), text, fill='white', align='center', anchor='ma', font=font)

    return await target.reply_photo(image_bytes_io(image, ext='png'),
                                    reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None)


# width / height
text_box_ratio = 3.8


def make_args(d: dict):
    return ": ".join(f"{k}={v}" for k, v in d.items())


def pad(color: str, border: int, with_text_box: bool = False) -> str:
    text_box = f"+ow/{text_box_ratio}" if with_text_box else ''
    args = {
        "width": f"iw+{border * 2}",
        "height": f"ih+{border * 2}{text_box}",
        "x": f"{border}",
        "y": f"{border}",
        "color": color,
    }
    return f"pad={make_args(args)}"


def drawtexts(text, width):
    rows = fill_rows(text, width)
    font_size = f"(w/{text_box_ratio})/3.6"
    for i, row in enumerate(rows):
        row = row.replace(":", "\\\\\\\\\\:")
        args = {
            "text": f"'{row}'",
            "fontsize": font_size,
            "fontfile": str(times_new_roman_font),
            "x": "(w-text_w)/2",
            "y": f"(h-(w/{text_box_ratio}))+({font_size})*{i}+5",
            "fontcolor": "white",
        }
        yield f"drawtext={make_args(args)}"


def fill_rows(text, width):
    char_width = width / 25
    rows = []
    for text in text.splitlines():
        if text.isspace():
            continue
        row = []
        row_len = 0
        for text in text.split(" "):
            if (row_len + len(text)) * char_width < width - 5:
                row.append(text)
                row_len += len(text) + 1
            else:
                rows.append(" ".join(row))
                row_len = len(text) + 1
                row = [text]
        if len(row) > 0:
            rows.append(" ".join(row))
    return rows


def demotivator_video(file: io.BytesIO, width: int, text: str) -> io.BytesIO:
    # text = '\n'.join(cut_long_text_yield(text, hard_max_len=40))[:1024].replace('\n\n', '\n')

    parameters = [
        '-vf', ",".join([
            pad("black", 4),
            pad("white", 4),
            pad("black", max(int(width * 0.07), 20), with_text_box=True),
            *drawtexts(text, width)
        ]),
        '-f', 'mp4',
    ]
    result = ffmpeg(file, parameters=parameters, out_suffix='.mp4')
    return result


async def process_demotivator_video(message: Message, meta: MetaInfo):
    target, file = await meta.extract_video()
    if file is None:
        return True
    if file.file_size > megabytes(20):
        return await message.reply('🤷🏻‍♂️ Файл больше 20 Мб, не смогу скачать')
    io_bytes = await file.download(destination_file=FakeBytesIO())

    _, text = meta.extract_text()
    if not text:
        return True

    video, timeouted = await cpu_executor.run(demotivator_video, io_bytes, getattr(file, 'width', 384), text)
    if timeouted:
        return await message.reply('🤷🏻‍♂️ Timeout')
    if video is None:
        return await message.reply('🤷🏻‍♂️ Что-то пошло не так')

    return await target.reply_video(video, reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None)


async def process_atmta(message: Message, meta: MetaInfo):
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    percent = 0.5
    if meta.arguments:
        try:
            percent = min(1., max(0., float(meta.arguments[0])))
        except ValueError:
            pass
    if percent <= 0:
        return await message.reply('Укажи долю больше 0 и не больше 1.')

    async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
        image = Image.open(file).convert('RGBA')
        crop_size = max(1, int(image.width * percent))

        im1 = image.crop((0, 0, crop_size, image.height))
        im2 = im1.transpose(Image.FLIP_LEFT_RIGHT)
        dst = Image.new('RGBA', (crop_size * 2, image.height))
        dst.paste(im1, (0, 0))
        dst.paste(im2, (im1.width, 0))

    return await target.reply_photo(image_bytes_io(dst, ext='png'),
                                    reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None)


async def process_atmta_v(message: Message, meta: MetaInfo):
    target, file = await meta.extract_image_with_downloading(with_profile_photo=True)
    if file is None:
        return True

    percent = 0.5
    if meta.arguments:
        try:
            percent = min(1., max(0., float(meta.arguments[0])))
        except ValueError:
            pass
    if percent <= 0:
        return await message.reply('Укажи долю больше 0 и не больше 1.')

    async with ChatActioner(message.chat, action_by_type(ContentType.PHOTO)):
        image = Image.open(file).convert('RGBA')
        crop_size = max(1, int(image.height * percent))

        im1 = image.crop((0, 0, image.width, crop_size))
        im2 = im1.transpose(Image.FLIP_TOP_BOTTOM)
        dst = Image.new('RGBA', (image.width, crop_size * 2))
        dst.paste(im1, (0, 0))
        dst.paste(im2, (0, im1.height))

    return await target.reply_photo(image_bytes_io(dst, ext='png'),
                                    reply_markup=rate_keyboard() if message.chat.type != ChatType.PRIVATE else None)
