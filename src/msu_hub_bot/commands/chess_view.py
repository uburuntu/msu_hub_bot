"""Chess questions, continuations and answer pages within one photo caption."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.utils.formatting import Bold, Code, Text, TextLink

from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT, PAGE_SIZE, View, compact, user_label
from msu_hub_bot.providers.chess import Puzzle

SOLUTION_PAGE_LIMIT = 500


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
            "Завершить может любой; автоматически — через 10 минут после появления доски.",
        )
    answer = next(option.label for option in puzzle.options if option.uci == puzzle.solution[0])
    summary = f"Угадали {sum(player.correct for player in players)} из {len(players)}." if players else "В этот раз никто не ответил."
    if scored is None:
        points = "Записываю очки…"
    elif scored:
        points = "Верно: +1, ошибка: −1. Минимум за день — 0."
    else:
        points = "Не удалось подтвердить запись очков."
    return Text("♟ Правильный ход: ", Bold(compact(answer, 80)), f".\n{summary}\n{points}")


def _solution_pages(puzzle: Puzzle) -> list[str]:
    """Keep every SAN move and its number, splitting only between complete turns."""
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
    pages = [""]
    for turn in turns:
        if pages[-1] and len(Text(pages[-1], " ", turn)) > SOLUTION_PAGE_LIMIT:
            pages.append("")
        pages[-1] += (" " if pages[-1] else "") + turn
    return pages


def _footer(puzzle: Puzzle, *, closed: bool, page: int, pages: int) -> Text:
    source = Text(TextLink("Задача на Lichess", url=f"https://lichess.org/training/{puzzle.id}")) if closed else Text()
    navigation = Text(f"Страница {page + 1}/{pages}") if pages > 1 else Text()
    return Text(source, "\n", navigation) if len(source) and len(navigation) else Text(source, navigation)


def render(puzzle: Puzzle, players: Sequence[Player], *, closed: bool, scored: bool | None = True, page: int = 0) -> View:
    """Show the full continuation first, then every player's answer in bounded pages."""
    solutions = _solution_pages(puzzle) if closed else []
    player_pages = (len(players) + PAGE_SIZE - 1) // PAGE_SIZE
    pages = max(1, len(solutions) + player_pages)
    page = min(max(0, page), pages - 1)
    header = _header(puzzle, players, closed=closed, scored=scored)
    footer = _footer(puzzle, closed=closed, page=page, pages=pages)
    if page < len(solutions):
        body = Text("Продолжение:\n", Code(solutions[page]))
    else:
        start = (page - len(solutions)) * PAGE_SIZE
        selected = players[start : start + PAGE_SIZE]
        title = "Ответы:\n" if closed else ""
        row_budget = (CAPTION_LIMIT - len(header) - len(footer) - len(title) - 4) // max(1, len(selected)) - 1
        rows: list[Text] = []
        for player in selected:
            label = user_label(player.user_id, player.name, player.username)
            if closed:
                result = ("✓ +1 " if player.correct else "✗ −1 ") if scored else ("✓ " if player.correct else "✗ ")
                move_budget = min(80, row_budget - len(label) - len(result) - 3)
                rows.append(Text(result, label, " — ", compact(player.move or "Неизвестный ход", move_budget)))
            else:
                rows.append(label)
        body = Text(title, *[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
        if not players and not closed:
            body = Text("Пока никто не ответил. Твой ход!")
    text = Text(header, "\n\n", body)
    if len(footer):
        text = Text(text, "\n\n", footer)
    caption, entities = text.render()
    return View(caption, entities, page, pages)
