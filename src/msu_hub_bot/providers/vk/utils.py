import re
from urllib.parse import ParseResult, urlparse

from msu_hub_bot.utils import shorten

pattern_wiki_link = re.compile(r"\[([^ |\n]+)\|([^\]\n]+)\]", re.U)
pattern_hashtag = re.compile(r"(#\S+)@\S+", re.U)
pattern_link = re.compile(r"(http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+)", re.U)
escaping_symbols = str.maketrans({"<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;"})


def cut_hashtags(text: str) -> str:
    text = pattern_hashtag.sub(r"\1", text)
    return text


def escape_symbols(text: str) -> str:
    text = text.translate(escaping_symbols)
    return text


def cut_long_links(text: str, width: int = 80) -> str:
    def repl(match):
        if len(url := match.group(1)) > width:
            return href(url, shorten(url, width=width))
        return url

    return pattern_link.sub(repl, text)


def replace_wiki_links(text: str, raw_link: bool = False) -> str:
    link_format_1 = "{1} ({0})" if raw_link else '<a href="{0}">{1}</a>'
    link_format_2 = "{1} (vk.com/{0})" if raw_link else '<a href="https://vk.com/{0}">{1}</a>'
    results = pattern_wiki_link.findall(text)
    for link, link_text in results:
        before = "[{0}|{1}]".format(link, link_text)
        if "vk.com" in link:
            after = link_format_1.format(link, link_text)
        else:
            after = link_format_2.format(link, link_text)
        text = text.replace(before, after)

    return text


def prepare_vk_text(text: str):
    return replace_wiki_links(cut_long_links(escape_symbols(cut_hashtags(text))))


def href(url: str, text: str = None, url_cut_width: int = 32) -> str:
    text = text or shorten(url, width=url_cut_width)
    return f'<a href="{url}">{text}</a>'


def check_vk_url(url: str):
    result: ParseResult = urlparse(url)
    return result.netloc == "vk.com" and result.path.startswith("/wall"), result.path.replace("/wall", "")
