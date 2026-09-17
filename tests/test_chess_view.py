"""Photo-caption limits, hidden choices and complete paged chess results."""

from dataclasses import replace

import pytest
from aiogram.utils.formatting import Text

from msu_hub_bot.commands import chess_view
from msu_hub_bot.commands.chess_view import Player, render
from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT, PAGE_SIZE, compact
from msu_hub_bot.providers.chess import MoveOption, Puzzle

PUZZLE = Puzzle(
    id="X0FOH",
    fen="rkb2R2/p1p4p/1pB1p3/2n1q3/8/P1p5/1PP3PP/1K3R2 w - - 0 21",
    solution=("f8c8", "b8c8", "f1f8"),
    options=(MoveOption("f8c8", "Ладья f8 × c8"), MoveOption("f8f7", "Ладья f8 → f7")),
    line=("Rxc8+", "Kxc8", "Rf8#"),
)


def validate_caption(view):
    encoded = view.caption.encode("utf-16-le")
    assert 0 < len(encoded) // 2 <= CAPTION_LIMIT
    assert len(view.entities) <= 6
    for entity in view.entities:
        assert 0 <= entity.offset < len(encoded) // 2
        assert entity.length > 0 and entity.offset + entity.length <= len(encoded) // 2
        # UTF-16 slicing must land on complete characters, including emoji names.
        extracted = encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
        assert extracted
    assert Text.from_entities(view.caption, view.entities).render()[0] == view.caption


@pytest.mark.parametrize("side,word", [("w", "белых"), ("b", "чёрных")])
def test_question_shows_side_and_rules_but_hides_solution_and_source(side, word):
    puzzle = replace(PUZZLE, fen=PUZZLE.fen.replace(" w ", f" {side} "))
    view = render(puzzle, [], closed=False)
    assert f"Ход {word}" in view.caption
    assert "10 минут" in view.caption and "Завершить может любой" in view.caption
    assert "Пока никто не ответил" in view.caption
    assert all(value not in view.caption for value in (*puzzle.line, puzzle.id, "Lichess", puzzle.solution[0]))
    assert all(entity.type != "text_link" for entity in view.entities)
    assert view.page == 0 and view.pages == 1
    validate_caption(view)


def test_active_participants_have_no_hint_of_their_choice():
    first = [Player(1, "Игрок", "player", "Ладья f8 × c8", True)]
    second = [replace(first[0], move="Ладья f8 → f7", correct=False)]
    assert render(PUZZLE, first, closed=False) == render(PUZZLE, second, closed=False)


@pytest.mark.parametrize("closed", [False, True])
def test_many_emoji_names_and_hostile_markup_remain_bounded_and_every_player_is_reachable(closed):
    players = [Player(uid, f"{uid} <b>&\n" + "🧑🏽‍🚀" * 40, "username" * 10, "Ладья f8 × c8", uid % 2 == 0) for uid in range(1, 154)]
    first = render(PUZZLE, players, closed=closed)
    assert first.pages == (len(players) + PAGE_SIZE - 1) // PAGE_SIZE + int(closed)
    mentions = []
    for page in range(first.pages):
        view = render(PUZZLE, players, closed=closed, page=page)
        validate_caption(view)
        assert view.page == page and view.pages == first.pages
        if closed:
            assert "Угадали 76 из 153" in view.caption
            assert any(entity.url == f"https://lichess.org/training/{PUZZLE.id}" for entity in view.entities)
        mentions.extend(entity.url for entity in view.entities if entity.url and entity.url.startswith("tg://user?id="))
    assert mentions == [f"tg://user?id={player.user_id}" for player in players]
    assert render(PUZZLE, players, closed=closed, page=-9) == first
    assert render(PUZZLE, players, closed=closed, page=999_999).page == first.pages - 1


def test_final_solution_is_numbered_for_both_sides_and_retains_source():
    white = render(PUZZLE, [], closed=True)
    assert "21. Rxc8+ Kxc8 22. Rf8#" in white.caption
    black_puzzle = replace(PUZZLE, fen=PUZZLE.fen.replace(" w ", " b "))
    black = render(black_puzzle, [], closed=True)
    assert "21... Rxc8+ 22. Kxc8 Rf8#" in black.caption
    for view in (white, black):
        assert "Правильный ход: Ладья f8 × c8" in view.caption
        assert "В этот раз никто не ответил" in view.caption
        assert view.pages == 1
        assert any(entity.type == "code" for entity in view.entities)
        assert any(entity.url == "https://lichess.org/training/X0FOH" for entity in view.entities)
        validate_caption(view)


def test_all_forty_plies_survive_solution_pagination_and_player_pages(monkeypatch):
    monkeypatch.setattr(chess_view, "SOLUTION_PAGE_LIMIT", 100)
    puzzle = replace(PUZZLE, fen=PUZZLE.fen.rsplit(" ", 1)[0] + " 490", line=tuple(["Nfxe6+", "gxh1=Q+"] * 20))
    players = [Player(uid, f"Игрок {uid}", None, "Ладья f8 × c8", True) for uid in range(5)]
    first = render(puzzle, players, closed=True)
    solutions = []
    mentions = []
    for page in range(first.pages):
        view = render(puzzle, players, closed=True, page=page)
        validate_caption(view)
        solutions.extend(entity.extract_from(view.caption) for entity in view.entities if entity.type == "code")
        mentions.extend(entity.url for entity in view.entities if entity.url and entity.url.startswith("tg://"))
    expected = " ".join(f"{number}. Nfxe6+ gxh1=Q+" for number in range(490, 510))
    assert len(solutions) > 1
    assert " ".join(solutions) == expected
    assert mentions == [f"tg://user?id={player.user_id}" for player in players]
    assert first.pages == len(solutions) + 2


def test_player_pages_show_every_chosen_move_and_the_scoring_rules():
    players = [Player(1, "Правый", None, "Ладья f8 × c8", True), Player(2, "Левый", "left", "Ладья f8 → f7", False)]
    view = render(PUZZLE, players, closed=True, page=1)
    assert "Угадали 1 из 2" in view.caption
    assert "✓ +1 Правый — Ладья f8 × c8" in view.caption
    assert "✗ −1 Левый (@left) — Ладья f8 → f7" in view.caption
    assert "Минимум за день — 0" in view.caption
    validate_caption(view)


@pytest.mark.parametrize("scored,expected", [(None, "Записываю очки…"), (False, "Не удалось подтвердить запись очков.")])
def test_unconfirmed_scores_do_not_claim_awarded_points(scored, expected):
    players = [Player(1, "Игрок", None, "Ладья f8 × c8", True)]
    for page in (0, 1):
        view = render(PUZZLE, players, closed=True, scored=scored, page=page)
        assert expected in view.caption
        assert "+1" not in view.caption and "−1" not in view.caption
        validate_caption(view)


def test_only_visible_names_are_formatted_and_markup_stays_literal(monkeypatch):
    players = [Player(uid, '<a href="https://example.org">🧑</a>\n&', "handle", "Ладья", True) for uid in range(300)]
    original = chess_view.user_label
    calls = []

    def recording_label(user_id, name, username):
        calls.append(user_id)
        return original(user_id, name, username)

    monkeypatch.setattr(chess_view, "user_label", recording_label)
    view = render(PUZZLE, players, closed=True, page=5)
    assert calls == [16, 17, 18, 19]
    assert '<a href="https://example.org">' in view.caption
    assert all(entity.url != "https://example.org" for entity in view.entities)
    assert compact(players[16].name, 48) in view.caption
    validate_caption(view)


def test_long_move_labels_share_remaining_caption_budget():
    puzzle = replace(PUZZLE, options=(replace(PUZZLE.options[0], label="🧑" * 100),))
    players = [Player(uid, "🧑" * 129, "u" * 80, "🧑" * 300, True) for uid in range(4)]
    for page in (0, 1):
        view = render(puzzle, players, closed=True, page=page)
        validate_caption(view)
    assert len([entity for entity in view.entities if entity.url and entity.url.startswith("tg://")]) == 4
