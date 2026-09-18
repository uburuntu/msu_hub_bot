"""Chess rules, clock boundaries and persistence use no Telegram or network."""

import chess
import pytest

from msu_hub_bot.games.chess_play.models import Game, GameError, Player

WHITE = Player(user_id=11, name="Белый", username="white")
BLACK = Player(user_id=22, name="Чёрный", username="black")
VIEWER = Player(user_id=33, name="Зритель")


def invitation(**kwargs):
    return Game(token="game", bot_id=123, chat_id=-100, white=WHITE, created_at=1000, invite_deadline=1600, **kwargs)


def playing(**kwargs):
    game = invitation(**kwargs)
    game.join(BLACK, now=1100)
    return game


def play(game, moves, start=1100):
    for index, uci in enumerate(moves):
        user_id = WHITE.user_id if game.board().turn == chess.WHITE else BLACK.user_id
        game.move(user_id, uci, now=start + index)


def test_invitation_clock_starts_on_first_other_player_join():
    game = invitation()
    assert game.status == "waiting"
    assert game.remaining(1599) == (600, 600)
    assert game.deadline() == 1600
    with pytest.raises(GameError, match="другой участник"):
        game.join(WHITE, 1500)
    assert game.revision == 0
    game.join(BLACK, 1590)
    assert game.status == "playing"
    assert game.turn_player == WHITE
    assert game.deadline() == 2190
    assert game.revision == 1
    with pytest.raises(GameError, match="занято"):
        game.join(VIEWER, 1591)
    assert game.black == BLACK
    assert game.turn_started == 1590


def test_move_consumes_only_movers_clock_and_adds_five_seconds():
    game = playing()
    game.move(WHITE.user_id, "e2e4", 1142.5)
    assert game.remaining(1142.5) == (562.5, 600)
    assert game.remaining(1152.5) == (562.5, 590)
    assert game.deadline() == 1742.5
    game.move(BLACK.user_id, "c7c5", 1172.5)
    assert game.remaining(1172.5) == (562.5, 575)
    assert game.deadline() == 1735
    assert game.moves == ["e2e4", "c7c5"]
    assert game.revision == 3


def test_selection_back_and_draw_offer_never_pause_clock_or_grant_increment():
    game = playing()
    game.select(WHITE.user_id, "e2", 1120)
    game.select(WHITE.user_id, None, 1130)
    game.offer_draw(WHITE.user_id, 1140)
    game.decline_draw(BLACK.user_id, 1150)
    assert game.remaining(1150) == (550, 600)
    assert game.deadline() == 1700
    assert game.white_seconds == 600
    assert game.revision == 5
    game.remaining(1200)
    assert game.revision == 5


@pytest.mark.parametrize("user_id,uci", [(33, "e2e4"), (22, "e2e4"), (11, "e2e5"), (11, "bad"), (11, "0000")])
def test_invalid_actions_do_not_mutate_or_grant_increment(user_id, uci):
    game = playing()
    before = game.model_dump_json()
    with pytest.raises(GameError):
        game.move(user_id, uci, 1130)
    assert game.model_dump_json() == before
    assert game.remaining(1130) == (570, 600)


@pytest.mark.parametrize("square", ["bad", "a7", "a1", "e4"])
def test_only_own_movable_pieces_can_be_selected(square):
    game = playing()
    with pytest.raises(GameError):
        game.select(WHITE.user_id, square, 1120)
    assert game.revision == 1
    assert game.selected is None


def test_selection_is_not_changed_by_viewer_or_other_player():
    game = playing()
    game.select(WHITE.user_id, "g1", 1101)
    for user in (VIEWER, BLACK):
        with pytest.raises(GameError):
            game.select(user.user_id, "b1", 1102)
    assert game.selected == "g1"
    assert game.revision == 2
    game.select(WHITE.user_id, "g1", 1103)
    assert game.revision == 2


def test_duplicate_move_cannot_grant_extra_time():
    game = playing()
    game.move(WHITE.user_id, "g1f3", 1105)
    assert game.white_seconds == 600
    with pytest.raises(GameError, match="ход соперника"):
        game.move(WHITE.user_id, "g1f3", 1106)
    assert game.moves == ["g1f3"]
    assert game.white_seconds == 600
    assert game.revision == 2


@pytest.mark.parametrize("now", [1700, 1700.1, 2000])
def test_clock_loss_at_exact_zero_and_later_cannot_be_saved_by_move(now):
    game = playing()
    with pytest.raises(GameError, match="завершена"):
        game.move(WHITE.user_id, "e2e4", now)
    assert game.result == "timeout"
    assert game.winner == BLACK.user_id
    assert game.remaining(now + 1000) == (0, 600)
    assert game.moves == []
    assert game.deadline() is None
    assert game.revision == 2
    assert not game.expire(now + 1000)


def test_move_just_before_deadline_is_accepted_and_gets_increment():
    game = playing()
    game.move(WHITE.user_id, "e2e4", 1699.75)
    assert game.white_seconds == 5.25
    assert game.status == "playing"
    assert game.deadline() == 2299.75


def test_black_timeout_awards_white_and_freezes_both_clocks():
    game = playing()
    game.move(WHITE.user_id, "e2e4", 1130)
    assert game.expire(1730)
    assert game.winner == WHITE.user_id
    assert game.remaining(2000) == (575, 0)


def test_timeout_is_draw_if_opponent_has_only_king():
    game = playing(initial_fen="7k/8/8/8/8/8/2R5/K7 w - - 0 1")
    assert game.board().has_insufficient_material(chess.BLACK)
    assert game.expire(1700)
    assert game.result == "timeout_insufficient_material"
    assert game.winner is None
    assert Game.model_validate_json(game.model_dump_json()) == game


def test_restart_retains_absolute_clock_deadline_and_original_turn():
    game = playing()
    game.move(WHITE.user_id, "e2e4", 1130)
    game.select(BLACK.user_id, "e7", 1150)
    restored = Game.model_validate_json(game.model_dump_json())
    assert restored.remaining(1230) == (575, 500)
    assert restored.deadline() == 1730
    assert restored.turn_player == BLACK
    assert restored.selected == "e7"
    assert restored.expire(1730)
    assert restored.winner == WHITE.user_id


def test_checkmate_winner_and_clocks_remain_fixed():
    game = playing()
    play(game, ["f2f3", "e7e5", "g2g4", "d8h4"], 1110)
    assert game.result == "checkmate"
    assert game.winner == BLACK.user_id
    assert game.status == "finished"
    assert game.remaining(9999) == (599, 608)
    assert game.revision == 5
    with pytest.raises(GameError, match="завершена"):
        game.resign(BLACK.user_id, 1114)
    assert game.winner == BLACK.user_id


def test_stalemate_is_draw():
    game = playing(initial_fen="7k/5K2/8/6Q1/8/8/8/8 w - - 0 1")
    game.move(WHITE.user_id, "g5g6", 1101)
    assert game.result == "stalemate"
    assert game.winner is None


def test_insufficient_material_after_capture_is_draw():
    game = playing(initial_fen="7k/8/8/8/8/8/1r6/K7 w - - 0 1")
    game.move(WHITE.user_id, "a1b2", 1101)
    assert game.result == "insufficient_material"
    assert game.winner is None


def test_history_restore_preserves_castling_and_en_passant():
    castle = playing()
    play(castle, ["g1f3", "g8f6", "g2g3", "g7g6", "f1g2", "f8g7"])
    castle = Game.model_validate_json(castle.model_dump_json())
    castle.move(WHITE.user_id, "e1g1", 1106)
    assert castle.board().piece_at(chess.F1) == chess.Piece(chess.ROOK, chess.WHITE)
    assert castle.board().king(chess.WHITE) == chess.G1
    en_passant = playing()
    play(en_passant, ["e2e4", "a7a6", "e4e5", "d7d5"])
    en_passant = Game.model_validate_json(en_passant.model_dump_json())
    en_passant.move(WHITE.user_id, "e5d6", 1104)
    assert en_passant.board().piece_at(chess.D5) is None
    assert en_passant.board().piece_at(chess.D6) == chess.Piece(chess.PAWN, chess.WHITE)


@pytest.mark.parametrize("promotion,piece", [("q", chess.QUEEN), ("r", chess.ROOK), ("b", chess.BISHOP), ("n", chess.KNIGHT)])
def test_promotion_keeps_all_four_legal_choices(promotion, piece):
    game = playing(initial_fen="7k/P7/8/8/8/8/7p/7K w - - 0 1")
    with pytest.raises(GameError):
        game.move(WHITE.user_id, "a7a8", 1101)
    game.move(WHITE.user_id, "a7a8" + promotion, 1102)
    assert game.board().piece_at(chess.A8) == chess.Piece(piece, chess.WHITE)
    assert game.white_seconds == 603


def test_history_restores_claimable_repetition_and_fivefold_auto_draw():
    cycle = ["g1f3", "g8f6", "f3g1", "f6g8"]
    game = playing()
    play(game, cycle * 2)
    restored = Game.model_validate_json(game.model_dump_json())
    assert restored.board().can_claim_threefold_repetition()
    restored.claim_draw(WHITE.user_id, 1110)
    assert restored.result == "threefold_repetition"
    assert restored.winner is None
    play(game, cycle * 2, start=1110)
    assert game.result == "fivefold_repetition"
    assert game.winner is None


def test_fifty_move_claim_and_seventyfive_move_automatic_draw():
    claimed = playing(initial_fen="7k/8/8/8/8/8/R7/K7 w - - 100 80")
    claimed.claim_draw(WHITE.user_id, 1101)
    assert claimed.result == "fifty_moves"
    automatic = playing(initial_fen="7k/8/8/8/8/8/R7/K7 w - - 149 80")
    automatic.move(WHITE.user_id, "a2b2", 1101)
    assert automatic.result == "seventyfive_moves"


def test_draw_claim_rejected_before_available_or_out_of_turn():
    game = playing()
    for user_id in (WHITE.user_id, BLACK.user_id, VIEWER.user_id):
        with pytest.raises(GameError):
            game.claim_draw(user_id, 1101)
    assert game.revision == 1
    assert game.status == "playing"


def test_draw_offer_requires_opponents_consent_and_clock_keeps_running():
    game = playing()
    with pytest.raises(GameError):
        game.offer_draw(VIEWER.user_id, 1110)
    game.offer_draw(WHITE.user_id, 1120)
    for user_id in (WHITE.user_id, VIEWER.user_id):
        with pytest.raises(GameError):
            game.accept_draw(user_id, 1125)
    game.accept_draw(BLACK.user_id, 1130)
    assert game.result == "agreed_draw"
    assert game.winner is None
    assert game.remaining(9999) == (570, 600)
    assert game.draw_offer is None


def test_draw_offer_remains_after_own_move_but_opponents_move_declines_it():
    game = playing()
    game.offer_draw(WHITE.user_id, 1101)
    game.move(WHITE.user_id, "e2e4", 1102)
    assert game.draw_offer == WHITE.user_id
    game.move(BLACK.user_id, "e7e5", 1103)
    assert game.draw_offer is None
    with pytest.raises(GameError):
        game.accept_draw(BLACK.user_id, 1104)


def test_resignation_freezes_clocks_and_names_opponent_winner():
    game = playing()
    with pytest.raises(GameError):
        game.resign(VIEWER.user_id, 1120)
    game.resign(WHITE.user_id, 1150)
    assert game.result == "resigned"
    assert game.winner == BLACK.user_id
    assert game.remaining(2000) == (550, 600)


def test_expiration_precedes_draw_acceptance_or_resignation():
    for action in ("accept_draw", "resign"):
        game = playing()
        game.offer_draw(WHITE.user_id, 1699)
        with pytest.raises(GameError, match="завершена"):
            getattr(game, action)(BLACK.user_id, 1700)
        assert game.result == "timeout"
        assert game.winner == BLACK.user_id


def test_invitation_can_only_be_cancelled_by_creator_and_expires_at_boundary():
    game = invitation()
    with pytest.raises(GameError):
        game.cancel(VIEWER.user_id, 1100)
    game.cancel(WHITE.user_id, 1101)
    assert game.result == "cancelled"
    assert game.remaining(9999) == (600, 600)
    late = invitation()
    with pytest.raises(GameError, match="завершена"):
        late.join(BLACK, 1600)
    assert late.result == "invite_expired"
    assert late.black is None
    assert late.revision == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_started": None},
        {"turn_started": float("nan")},
        {"black": WHITE.model_dump()},
        {"initial_fen": "not a position"},
        {"initial_fen": "8/8/8/8/8/8/8/8 w - - 0 1"},
        {"moves": ["e2e5"]},
        {"moves": ["0000"]},
        {"selected": "a7"},
        {"selected": "invalid"},
        {"draw_offer": VIEWER.user_id},
        {"winner": WHITE.user_id},
        {"white_seconds": -1},
        {"invite_deadline": 999},
    ],
)
def test_inconsistent_saved_game_is_rejected_before_recovery(changes):
    state = playing().model_dump()
    state.update(changes)
    with pytest.raises(ValueError):
        Game.model_validate(state)


@pytest.mark.parametrize(
    "changes",
    [
        {"winner": VIEWER.user_id},
        {"winner": None},
        {"result": "agreed_draw"},
        {"result": "unknown"},
        {"turn_started": 1150},
        {"draw_offer": WHITE.user_id},
        {"selected": "e2"},
    ],
)
def test_inconsistent_saved_finished_game_is_rejected(changes):
    game = playing()
    game.resign(WHITE.user_id, 1130)
    state = game.model_dump()
    state.update(changes)
    with pytest.raises(ValueError):
        Game.model_validate(state)


def test_finished_game_and_cancelled_invitation_roundtrip_preserve_result():
    game = playing()
    play(game, ["f2f3", "e7e5", "g2g4", "d8h4"], 1110)
    assert Game.model_validate_json(game.model_dump_json()) == game
    invitation_ = invitation()
    invitation_.cancel(WHITE.user_id, 1120)
    assert Game.model_validate_json(invitation_.model_dump_json()) == invitation_


@pytest.mark.parametrize(
    "fen,expected",
    [
        ("1b5k/8/8/8/8/8/6Q1/K7 w - - 0 1", "timeout_insufficient_material"),
        ("1b5k/8/8/8/8/8/2P5/K7 w - - 0 1", "timeout"),
    ],
)
def test_flagfall_uses_opponents_actual_mating_material(fen, expected):
    state = playing(initial_fen=fen)
    state.expire(1700)
    assert state.result == expected
    assert state.winner == (BLACK.user_id if expected == "timeout" else None)


def test_backwards_clock_does_not_charge_next_player_for_time_before_turn():
    state = playing()
    state.move(WHITE.user_id, "e2e4", 1090)
    assert state.turn_started == 1100
    assert state.remaining(1105) == (605, 595)
    assert state.deadline() == 1700


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1])
def test_invalid_clock_input_cannot_mutate_game(now):
    state = playing()
    before = state.model_dump_json()
    with pytest.raises(ValueError):
        state.move(WHITE.user_id, "e2e4", now)
    assert state.model_dump_json() == before


def test_unknown_payload_fields_survive_moves_nested_players_and_roundtrip():
    raw = invitation().model_dump()
    raw["community_tag"] = {"future": [None, 17, "value"]}
    raw["white"]["pronunciation"] = {"syllables": ["бе", "лый"]}
    state = Game.model_validate(raw)
    state.join(Player.model_validate({**BLACK.model_dump(), "badge": "new"}), 1100)
    state.move(WHITE.user_id, "e2e4", 1110)
    restored = Game.model_validate_json(state.model_dump_json()).model_dump()
    assert restored["community_tag"] == raw["community_tag"]
    assert restored["white"]["pronunciation"] == raw["white"]["pronunciation"]
    assert restored["black"]["badge"] == "new"


def test_rejected_atomic_transition_does_not_leave_a_partial_move():
    state = playing()
    state.revision = 2**31 - 1
    before = state.model_dump_json()
    with pytest.raises(ValueError):
        state.move(WHITE.user_id, "e2e4", 1110)
    assert state.model_dump_json() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "x" * 23},
        {"token": "cannot:pack"},
        {"bot_id": True},
        {"bot_id": WHITE.user_id},
        {"chat_id": 0},
        {"chat_id": False},
        {"thread_id": 0},
        {"message_id": -1},
        {"revision": 2**31},
        {"white_rating": -1_000_001},
        {"white_rating": 1_000_001},
        {"created_at": float("inf")},
        {"turn_started": 999},
        {"white_seconds": float("inf")},
    ],
)
def test_invalid_saved_boundaries_are_rejected_without_defaulting(changes):
    raw = playing().model_dump()
    raw.update(changes)
    with pytest.raises(ValueError):
        Game.model_validate(raw)


def test_bot_cannot_take_black_seat():
    state = invitation()
    with pytest.raises(GameError, match="человека"):
        state.join(Player(user_id=state.bot_id, name="Bot"), 1100)
    assert state.black is None
    assert state.revision == 0


@pytest.mark.parametrize("changes", [{"winner": WHITE.user_id}, {"result": "resigned"}])
def test_saved_checkmate_must_match_board_outcome(changes):
    state = playing()
    play(state, ["f2f3", "e7e5", "g2g4", "d8h4"])
    raw = state.model_dump()
    raw.update(changes)
    with pytest.raises(ValueError, match="disagrees"):
        Game.model_validate(raw)


@pytest.mark.parametrize(
    "changes", [{"winner": WHITE.user_id}, {"white_seconds": 1}, {"result": "timeout_insufficient_material", "winner": None}]
)
def test_saved_timeout_must_match_turn_clock_and_material(changes):
    state = playing()
    state.expire(1700)
    raw = state.model_dump()
    raw.update(changes)
    with pytest.raises(ValueError, match="flag fall"):
        Game.model_validate(raw)


def test_saved_history_cannot_continue_after_fivefold_automatic_draw():
    state = playing()
    raw = state.model_dump()
    raw["moves"] = ["g1f3", "g8f6", "f3g1", "f6g8"] * 4 + ["e2e4"]
    with pytest.raises(ValueError, match="automatic game ending"):
        Game.model_validate(raw)


def test_checkmate_precedes_seventyfive_move_rule():
    state = playing(initial_fen="7k/8/5KQ1/8/8/8/8/8 w - - 149 80")
    state.move(WHITE.user_id, "g6g7", 1100)
    assert state.result == "checkmate"
    assert state.winner == WHITE.user_id
    assert state.board().halfmove_clock == 150


def test_intended_next_move_can_make_a_draw_claim_available():
    state = playing()
    play(state, ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"])
    assert not state.board().is_repetition(3)
    state.claim_draw(BLACK.user_id, 1110)
    assert state.result == "threefold_repetition"
    assert len(state.moves) == 7
    fifty = playing(initial_fen="7k/8/8/8/8/8/R7/K7 w - - 99 80")
    fifty.claim_draw(WHITE.user_id, 1100)
    assert fifty.result == "fifty_moves"


def test_negative_global_rating_can_start_another_game():
    state = invitation(white_rating=-16)
    state.join(BLACK, 1100)
    state.black_rating = -8
    assert Game.model_validate_json(state.model_dump_json()) == state
