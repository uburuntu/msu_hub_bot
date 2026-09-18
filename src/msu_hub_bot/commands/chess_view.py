"""Chess questions, continuations and answer pages within one photo caption."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.utils.formatting import Bold, Code, Text, TextLink

from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT, PAGE_SIZE, View, compact, user_label, user_label_size
from msu_hub_bot.providers.chess import Puzzle


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    username: str | None
    move: str | None = None
    correct: bool = False


def _header(puzzle: Puzzle, players: Sequence[Player], *, closed: bool, scored: bool | None) -> Text:
    if not closed:
        side = "белых" if puzzle.fen.split()[1] == "w" else "чёрных"
        return Text(
            Bold(f"♟ Ход {side}. Найди лучший ход."),
            f"\n🗳 Ответили: {len(players)}.\n",
            "Выбор каждого покажу в конце.\n",
            "Завершить может любой; автоматически — через 10 минут после появления доски.\n",
            "Верно: +1, ошибка: −1. Минимум за день — 0.",
        )
    answer = next(option.label for option in puzzle.options if option.uci == puzzle.solution[0])
    summary = f"Угадали {sum(player.correct for player in players)} из {len(players)}." if players else "В этот раз никто не ответил."
    if scored is None:
        points = "Записываю очки…"
    elif scored:
        points = ""
    else:
        points = "Не удалось подтвердить запись очков."
    return Text("♟ Правильный ход: ", Bold(compact(answer, 80)), f".\n{summary}", f"\n{points}" if points else "")


def _solution_turns(puzzle: Puzzle) -> list[str]:
    """Keep every SAN move and its number, including a black first move."""
    move_number = int(puzzle.fen.split()[5])
    white = puzzle.fen.split()[1] == "w"
    turns: list[str] = []
    for san in puzzle.line:
        if white or not turns:
            turns.append(f"{move_number}{'.' if white else '...'} {san}")
        else:
            turns[-1] += f" {san}"
        if not white:
            move_number += 1
        white = not white
    return turns


@dataclass
class _ResultPage:
    continuation: str = ""
    start: int = 0
    stop: int = 0


def _result_prefix(player: Player, scored: bool | None) -> str:
    return ("✓ +1 " if player.correct else "✗ −1 ") if scored else ("✓ " if player.correct else "✗ ")


def _result_pages(puzzle: Puzzle, players: Sequence[Player], *, scored: bool | None, limit: int) -> list[_ResultPage]:
    """Pack complete turns and answer rows, creating entities only for the chosen page."""
    pages = [_ResultPage()]
    for turn in _solution_turns(puzzle):
        previous = pages[-1].continuation
        candidate = f"{previous} {turn}" if previous else turn
        if previous and len(Text("Продолжение:\n", candidate)) > limit:
            pages.append(_ResultPage(continuation=turn))
        else:
            pages[-1].continuation = candidate
    size = len(Text("Продолжение:\n", pages[-1].continuation)) if pages[-1].continuation else 0
    for index, player in enumerate(players):
        current = pages[-1]
        count = current.stop - current.start
        separator = "\n" if count else "\n\nОтветы:\n" if current.continuation else "Ответы:\n"
        row_size = user_label_size(player.name, player.username) + len(
            Text(_result_prefix(player, scored), " — ", compact(player.move or "Неизвестный ход", 80))
        )
        if count == PAGE_SIZE or size + len(Text(separator)) + row_size > limit:
            current = _ResultPage(start=index, stop=index)
            pages.append(current)
            size = len(Text("Ответы:\n"))
        else:
            size += len(Text(separator))
        current.stop = index + 1
        size += row_size
    return pages


def _result_body(layout: _ResultPage, players: Sequence[Player], *, scored: bool | None) -> Text:
    body = Text("Продолжение:\n", Code(layout.continuation)) if layout.continuation else Text()
    for index in range(layout.start, layout.stop):
        player = players[index]
        separator = "\n" if index > layout.start else "\n\nОтветы:\n" if layout.continuation else "Ответы:\n"
        body = Text(
            body,
            separator,
            _result_prefix(player, scored),
            user_label(player.user_id, player.name, player.username),
            " — ",
            compact(player.move or "Неизвестный ход", 80),
        )
    return body


def _footer(puzzle: Puzzle, *, closed: bool, page: int, pages: int) -> Text:
    source = Text(TextLink("Задача на Lichess", url=f"https://lichess.org/training/{puzzle.id}")) if closed else Text()
    navigation = Text(f"Страница {page + 1}/{pages}") if pages > 1 else Text()
    return Text(source, "\n", navigation) if len(source) and len(navigation) else Text(source, navigation)


def render(puzzle: Puzzle, players: Sequence[Player], *, closed: bool, scored: bool | None = True, page: int = 0) -> View:
    """Combine ordinary results; paginate overflow without losing moves or answers."""
    header = _header(puzzle, players, closed=closed, scored=scored)
    if closed:
        budget = CAPTION_LIMIT - len(header) - len(_footer(puzzle, closed=True, page=0, pages=1)) - 4
        layouts = _result_pages(puzzle, players, scored=scored, limit=budget)
        if len(layouts) > 1:
            # A conservative navigation width stays valid when narrower pages add a page.
            max_pages = max(1, len(players) + len(puzzle.line))
            footer = _footer(puzzle, closed=True, page=max_pages - 1, pages=max_pages)
            layouts = _result_pages(puzzle, players, scored=scored, limit=CAPTION_LIMIT - len(header) - len(footer) - 4)
        pages = len(layouts)
        page = min(max(0, page), pages - 1)
        body = _result_body(layouts[page], players, scored=scored)
    else:
        pages = max(1, (len(players) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        start = page * PAGE_SIZE
        selected = players[start : start + PAGE_SIZE]
        rows = [user_label(player.user_id, player.name, player.username) for player in selected]
        body = Text(*[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
        if not players:
            body = Text("Пока никто не ответил. Твой ход!")
    footer = _footer(puzzle, closed=closed, page=page, pages=pages)
    text = Text(header, "\n\n", body)
    if len(footer):
        text = Text(text, "\n\n", footer)
    caption, entities = text.render()
    return View(caption, entities, page, pages)
