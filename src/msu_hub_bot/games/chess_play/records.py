"""Persistent match envelopes and permanent bot-wide Elo records."""

import math
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from msu_hub_bot.storage.features import Payload

from .models import Game, UserID

INITIAL_RATING = 800
K_FACTOR = 32
RatingValue = Annotated[int, Field(strict=True, ge=-1_000_000, le=1_000_000)]
type RatingChange = tuple[tuple[RatingValue, RatingValue], tuple[RatingValue, RatingValue]]


class ChatMatch(Payload):
    active: str | None = None


class SavedMatch(Payload):
    game: Game
    source_message_id: int = Field(strict=True, gt=0, le=2**31 - 1)
    publication_due: AwareDatetime
    publication: Literal["publishing", "bound", "abandoned"] = "publishing"
    finished_at: AwareDatetime | None = None
    rating_status: Literal["pending", "settled", "skipped"] = "pending"
    ratings: RatingChange | None = None
    revealed: bool = False
    presentation_failed: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.publication_due.timestamp() < self.game.created_at:
            raise ValueError("Publication cannot expire before creation")
        if self.finished_at is not None and self.finished_at.timestamp() < self.game.created_at:
            raise ValueError("A match cannot finish before creation")
        if self.publication == "bound" and self.game.message_id is None:
            raise ValueError("A bound match needs its board message")
        if self.ratings is not None and (self.game.status != "finished" or self.game.black is None):
            raise ValueError("Only a completed two-player match has rating changes")
        if self.revealed and self.game.status != "finished":
            raise ValueError("Only a completed match can be revealed")
        if self.finished_at is not None and self.game.status != "finished":
            raise ValueError("Only a completed match has a closing time")
        if (self.game.status == "finished") != (self.finished_at is not None):
            raise ValueError("Completed matches require a closing time")
        if (self.rating_status == "settled") != (self.ratings is not None):
            raise ValueError("Settled matches require their committed rating changes")
        if self.rating_status == "skipped" and (self.game.status != "finished" or self.game.black is not None):
            raise ValueError("Only unplayed invitations skip rating settlement")
        if self.game.status == "finished" and self.game.black is None and self.rating_status != "skipped":
            raise ValueError("Unplayed invitations cannot await rating settlement")
        if self.publication == "abandoned" and (self.game.status != "finished" or self.game.message_id is not None):
            raise ValueError("Only an unbound completed invitation can be abandoned")
        if self.publication == "publishing" and (self.game.message_id is not None or self.game.status != "waiting"):
            raise ValueError("Unbound invitations cannot have moves or a result")
        if self.revealed and self.rating_status == "pending":
            raise ValueError("A final result waits for rating settlement")
        return self


class Rating(Payload):
    user_id: UserID
    name: str = Field(max_length=256)
    username: str | None = Field(default=None, max_length=64)
    rating: RatingValue = INITIAL_RATING


class RatedPlayer(Rating):
    rank: int | None = None


class RatingPage(BaseModel):
    players: tuple[RatedPlayer, ...]
    total: int
    page: int
    pages: int


def elo_delta(white_rating: int, black_rating: int, white_score: float) -> int:
    """Round symmetrically; a finished match applies its starting-rating delta."""
    if white_score not in (0.0, 0.5, 1.0):
        raise ValueError("Elo score must be 0, 0.5 or 1")
    difference = min(6400, max(-6400, black_rating - white_rating))
    expected = 1.0 / (1.0 + math.pow(10.0, difference / 400.0))
    delta = K_FACTOR * (white_score - expected)
    return math.floor(delta + 0.5) if delta >= 0 else -math.floor(-delta + 0.5)
