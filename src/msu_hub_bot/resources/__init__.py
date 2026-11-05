from pathlib import Path


def path(filename: str) -> Path:
    return (Path(__file__).parent / filename).absolute()


lobster_font = path("Lobster-Regular.ttf")
times_new_roman_font = path("LiberationSerif-Regular.ttf")
meme_font = path("LiberationSans-Regular.ttf")
ubuntu_mono_font = path("UbuntuMono-Regular.ttf")
emoji_font = path("NotoColorEmoji.ttf")

debate = path("debate.csv")
