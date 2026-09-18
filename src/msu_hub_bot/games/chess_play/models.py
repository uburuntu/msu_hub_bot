"""Serializable rules and clocks for a human-versus-human chess game."""

import math
from typing import Annotated, Literal, Self

import chess
from pydantic import Field, model_validator

from msu_hub_bot.storage.features import Payload

INITIAL_SECONDS = 600.0
INCREMENT_SECONDS = 5.0
MAX_REVISION = 2**31 - 1
Token = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,22}$")]
UserID = Annotated[int, Field(strict=True, gt=0, le=2**52 - 1)]
Result = Literal[
    "checkmate",
    "timeout",
    "timeout_insufficient_material",
    "resigned",
    "stalemate",
    "insufficient_material",
    "seventyfive_moves",
    "fivefold_repetition",
    "fifty_moves",
    "threefold_repetition",
    "agreed_draw",
    "cancelled",
    "invite_expired",
]


class GameError(ValueError):
    """An expected, user-facing rejection of a game action."""


class Player(Payload):
    user_id: UserID
    name: str = Field(max_length=256)
    username: str | None = Field(default=None, max_length=64)


class Game(Payload):
    token: Token
    bot_id: UserID
    chat_id: int = Field(strict=True, ge=-(2**52 - 1), le=2**52 - 1)
    thread_id: int | None = Field(default=None, strict=True, gt=0, le=2**31 - 1)
    message_id: int | None = Field(default=None, strict=True, gt=0, le=2**31 - 1)
    white: Player
    black: Player | None = None
    white_rating: int = Field(default=800, strict=True, ge=-1_000_000, le=1_000_000)
    black_rating: int = Field(default=800, strict=True, ge=-1_000_000, le=1_000_000)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    invite_deadline: float = Field(ge=0, allow_inf_nan=False)
    turn_started: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    white_seconds: float = Field(default=INITIAL_SECONDS, ge=0, le=1_000_000, allow_inf_nan=False)
    black_seconds: float = Field(default=INITIAL_SECONDS, ge=0, le=1_000_000, allow_inf_nan=False)
    moves: list[Annotated[str, Field(pattern=r"^[a-h][1-8][a-h][1-8][qrbn]?$")]] = Field(default_factory=list, max_length=20_000)
    initial_fen: str = Field(default=chess.STARTING_FEN, max_length=128)
    revision: int = Field(default=0, strict=True, ge=0, le=MAX_REVISION)
    selected: Annotated[str, Field(pattern=r"^[a-h][1-8]$")] | None = None
    draw_offer: UserID | None = None
    winner: UserID | None = None
    result: Result | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        board = self.board(validate_endings=True)
        if self.invite_deadline < self.created_at:
            raise ValueError("Invitation ends before it was created")
        if self.chat_id == 0:
            raise ValueError("Chat ID must be nonzero")
        participants = {self.white.user_id}
        if self.black is not None:
            if self.black.user_id == self.white.user_id:
                raise ValueError("A player cannot occupy both sides")
            participants.add(self.black.user_id)
        if self.bot_id in participants:
            raise ValueError("The bot cannot occupy a player seat")
        outcome = board.outcome(claim_draw=False)
        if self.status == "playing":
            if self.turn_started is None or self.winner is not None:
                raise ValueError("Playing games require a running clock and no winner")
            if self.turn_started < self.created_at or outcome is not None:
                raise ValueError("Playing games require a valid current turn")
            if self.draw_offer is not None and self.draw_offer not in participants:
                raise ValueError("Only a participant can offer a draw")
            if self.selected is not None:
                origin = chess.parse_square(self.selected)
                if not any(move.from_square == origin for move in board.legal_moves):
                    raise ValueError("Selected piece must have a legal move")
        else:
            if self.turn_started is not None or self.selected is not None or self.draw_offer is not None:
                raise ValueError("Waiting and finished games cannot have an active turn")
            if self.status == "waiting" and (self.moves or self.winner is not None):
                raise ValueError("Waiting games cannot have moves or a winner")
            if self.status == "waiting" and outcome is not None:
                raise ValueError("Invitations require an unfinished position")
        if self.status == "finished":
            if self.black is None:
                if self.result not in {"cancelled", "invite_expired"} or self.winner is not None or self.moves:
                    raise ValueError("Unplayed invitations cannot have a game result")
            elif self.result in {"checkmate", "timeout", "resigned"}:
                if self.winner not in participants:
                    raise ValueError("Decisive games require a participant winner")
            elif self.result in {
                "stalemate",
                "insufficient_material",
                "seventyfive_moves",
                "fivefold_repetition",
                "fifty_moves",
                "threefold_repetition",
                "agreed_draw",
                "timeout_insufficient_material",
            }:
                if self.winner is not None:
                    raise ValueError("Drawn games cannot have a winner")
            else:
                raise ValueError("Unknown game result")
            if outcome is not None:
                winner = self.white if outcome.winner == chess.WHITE else self.black
                expected_winner = winner.user_id if outcome.winner is not None and winner else None
                if self.result != outcome.termination.name.lower() or self.winner != expected_winner:
                    raise ValueError("Saved result disagrees with the chess position")
            elif self.result in {"checkmate", "stalemate", "insufficient_material", "seventyfive_moves", "fivefold_repetition"}:
                raise ValueError("Saved result requires a finished chess position")
            if self.result == "fifty_moves" and not board.can_claim_fifty_moves():
                raise ValueError("Saved fifty-move draw cannot be claimed")
            if self.result == "threefold_repetition" and not board.can_claim_threefold_repetition():
                raise ValueError("Saved repetition draw cannot be claimed")
            if self.result in {"timeout", "timeout_insufficient_material"}:
                timed_out = self.white_seconds if board.turn == chess.WHITE else self.black_seconds
                winning_color = not board.turn
                winner = self.white if winning_color == chess.WHITE else self.black
                insufficient = board.has_insufficient_material(winning_color)
                expected = None if insufficient else winner.user_id if winner else None
                if timed_out != 0 or self.winner != expected or (self.result == "timeout_insufficient_material") != insufficient:
                    raise ValueError("Saved flag fall disagrees with clocks or mating material")
        return self

    def _update(self, **changes: object) -> None:
        """Validate a whole transition before exposing any of its changed fields."""
        state = self.model_dump(round_trip=True)
        state.update(changes)
        updated = type(self).model_validate(state)
        self.__dict__.update(updated.__dict__)

    def _now(self, now: float) -> float:
        if not math.isfinite(now) or now < 0:
            raise ValueError("Clock timestamps must be finite and nonnegative")
        # A backwards wall-clock adjustment must not charge the next player for
        # time before this turn or move an invitation before its creation.
        return max(now, self.turn_started if self.turn_started is not None else self.created_at)

    @property
    def status(self) -> Literal["waiting", "playing", "finished"]:
        if self.result is not None:
            return "finished"
        return "playing" if self.black is not None else "waiting"

    def board(self, *, validate_endings: bool = False) -> chess.Board:
        # Full history retains castling, en passant and repetition information.
        board = chess.Board(self.initial_fen)
        if not board.is_valid():
            raise ValueError("Invalid starting chess position")
        for uci in self.moves:
            if validate_endings and board.outcome(claim_draw=False) is not None:
                raise ValueError("Moves cannot continue after an automatic game ending")
            move = board.parse_uci(uci)
            if not move:
                raise ValueError("Null moves are not permitted")
            board.push(move)
        return board

    @property
    def turn_player(self) -> Player | None:
        if self.status != "playing":
            return None
        return self.white if self.board().turn == chess.WHITE else self.black

    def remaining(self, now: float) -> tuple[float, float]:
        now = self._now(now)
        white, black = self.white_seconds, self.black_seconds
        if self.status == "playing" and self.turn_started is not None:
            elapsed = max(0.0, now - self.turn_started)
            if self.board().turn == chess.WHITE:
                white = max(0.0, white - elapsed)
            else:
                black = max(0.0, black - elapsed)
        return white, black

    def deadline(self) -> float | None:
        if self.status == "finished":
            return None
        if self.status == "waiting":
            return self.invite_deadline
        assert self.turn_started is not None
        seconds = self.white_seconds if self.board().turn == chess.WHITE else self.black_seconds
        return self.turn_started + seconds

    def _finish(self, result: Result, winner: int | None, now: float) -> None:
        white, black = self.remaining(now)
        self._update(
            white_seconds=white,
            black_seconds=black,
            result=result,
            winner=winner,
            turn_started=None,
            selected=None,
            draw_offer=None,
            revision=self.revision + 1,
        )

    def expire(self, now: float) -> bool:
        now = self._now(now)
        deadline = self.deadline()
        if deadline is None or now < deadline:
            return False
        if self.status == "waiting":
            self._finish("invite_expired", None, now)
        else:
            assert self.black is not None
            board = self.board()
            winner = self.black if board.turn == chess.WHITE else self.white
            if board.has_insufficient_material(not board.turn):
                self._finish("timeout_insufficient_material", None, now)
            else:
                self._finish("timeout", winner.user_id, now)
        return True

    def _open(self, now: float) -> None:
        self.expire(now)
        if self.status == "finished":
            raise GameError("Партия уже завершена.")

    def _participant(self, user_id: int, now: float) -> None:
        self._open(now)
        if self.black is None:
            raise GameError("Ждём соперника.")
        if user_id not in (self.white.user_id, self.black.user_id):
            raise GameError("Вы наблюдаете за партией.")

    def _turn(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        player = self.turn_player
        assert player is not None
        if player.user_id != user_id:
            raise GameError("Сейчас ход соперника.")

    def join(self, player: Player, now: float) -> None:
        now = self._now(now)
        self._open(now)
        if self.black is not None:
            raise GameError("Место соперника уже занято.")
        if player.user_id == self.white.user_id:
            raise GameError("Нужен другой участник: вы играете белыми.")
        if player.user_id == self.bot_id:
            raise GameError("За чёрных ждём человека.")
        self._update(black=player, turn_started=now, revision=self.revision + 1)

    def select(self, user_id: int, square: str | None, now: float) -> None:
        self._turn(user_id, now)
        if square is not None:
            try:
                origin = chess.parse_square(square)
            except ValueError as exc:
                raise GameError("Такой клетки нет.") from exc
            board = self.board()
            if not any(move.from_square == origin for move in board.legal_moves):
                raise GameError("Этой фигурой сейчас нельзя ходить.")
        if square != self.selected:
            self._update(selected=square, revision=self.revision + 1)

    def move(self, user_id: int, uci: str, now: float) -> None:
        now = self._now(now)
        self._turn(user_id, now)
        board = self.board()
        try:
            move = board.parse_uci(uci)
        except ValueError as exc:
            raise GameError("Такой ход сейчас невозможен.") from exc
        if not move or move not in board.legal_moves:
            raise GameError("Такой ход сейчас невозможен.")
        white, black = self.remaining(now)
        if board.turn == chess.WHITE:
            white += INCREMENT_SECONDS
        else:
            black += INCREMENT_SECONDS
        board.push(move)
        changes: dict[str, object] = dict(
            white_seconds=white,
            black_seconds=black,
            moves=[*self.moves, move.uci()],
            turn_started=now,
            selected=None,
            draw_offer=self.draw_offer if self.draw_offer == user_id else None,
            revision=self.revision + 1,
        )
        outcome = board.outcome(claim_draw=False)
        if outcome is not None:
            assert self.black is not None
            winner = None if outcome.winner is None else self.white if outcome.winner == chess.WHITE else self.black
            changes.update(
                result=outcome.termination.name.lower(),
                winner=winner.user_id if winner else None,
                turn_started=None,
                selected=None,
                draw_offer=None,
            )
        self._update(**changes)

    def resign(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        assert self.black is not None
        winner = self.black if user_id == self.white.user_id else self.white
        self._finish("resigned", winner.user_id, now)

    def cancel(self, user_id: int, now: float) -> None:
        self._open(now)
        if self.status != "waiting" or user_id != self.white.user_id:
            raise GameError("Отменить приглашение может только его автор.")
        self._finish("cancelled", None, now)

    def offer_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is not None:
            raise GameError("Предложение ничьей уже отправлено.")
        self._update(draw_offer=user_id, revision=self.revision + 1)

    def accept_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is None or self.draw_offer == user_id:
            raise GameError("Соперник не предлагал ничью.")
        self._finish("agreed_draw", None, now)

    def decline_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is None or self.draw_offer == user_id:
            raise GameError("Соперник не предлагал ничью.")
        self._update(draw_offer=None, revision=self.revision + 1)

    def claim_draw(self, user_id: int, now: float) -> None:
        self._turn(user_id, now)
        board = self.board()
        if board.can_claim_fifty_moves():
            self._finish("fifty_moves", None, now)
        elif board.can_claim_threefold_repetition():
            self._finish("threefold_repetition", None, now)
        else:
            raise GameError("Сейчас нельзя потребовать ничью по правилам.")
