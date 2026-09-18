"""Question providers and renderers for the shared quiz lifecycle."""

import asyncio
import random
from dataclasses import dataclass
from typing import Literal

from aiogram.types import BufferedInputFile

from msu_hub_bot.commands import chess_view, geoguess_view
from msu_hub_bot.commands.quiz_view import View
from msu_hub_bot.games.models import Question, RoundState, Vote
from msu_hub_bot.media.chessboard import render_board
from msu_hub_bot.providers.chess import MoveOption, Puzzle, random_puzzle
from msu_hub_bot.providers.geoguess import COUNTRIES, Photo, random_photo


def chess_puzzle(question: Question) -> Puzzle:
    if question.fen is None or not question.solution or len(question.moves) != 6:
        raise ValueError("Incomplete chess question")
    return Puzzle(
        id=question.identity,
        fen=question.fen,
        solution=tuple(question.solution),
        options=tuple(MoveOption(move, label) for move, label in zip(question.moves, question.choices, strict=True)),
        line=tuple(question.line),
    )


def geoguess_photo(question: Question) -> Photo:
    values = (question.country, question.city, question.url, question.source, question.author, question.license, question.license_url)
    if any(value is None for value in values):
        raise ValueError("Incomplete geography question")
    return Photo(*(value or "" for value in values))


@dataclass(frozen=True)
class Definition:
    name: Literal["chess", "geoguess"]

    async def load(self, recent: list[str]) -> Question:
        if self.name == "chess":
            puzzle = await random_puzzle(tuple(recent))
            return Question(
                kind="chess",
                identity=puzzle.id,
                choices=[option.label for option in puzzle.options],
                answer=next(index for index, option in enumerate(puzzle.options) if option.uci == puzzle.solution[0]),
                fen=puzzle.fen,
                solution=list(puzzle.solution),
                line=list(puzzle.line),
                moves=[option.uci for option in puzzle.options],
            )
        photo = await random_photo(tuple(recent))
        options = random.sample(sorted(set(COUNTRIES.values()) - {photo.country}), 5) + [photo.country]
        random.shuffle(options)
        return Question(
            kind="geoguess",
            identity=photo.country,
            choices=options,
            answer=options.index(photo.country),
            country=photo.country,
            city=photo.city,
            url=photo.url,
            source=photo.source,
            author=photo.author,
            license=photo.license,
            license_url=photo.license_url,
        )

    async def photo(self, question: Question, *, solution: bool = False) -> BufferedInputFile | str:
        if self.name == "geoguess":
            if question.url is None:
                raise ValueError("Missing geography photo")
            return question.url
        puzzle = chess_puzzle(question)
        image = await asyncio.to_thread(render_board, puzzle.fen, arrow=puzzle.solution[0] if solution else None)
        return BufferedInputFile(image, filename="chess-solution.png" if solution else "chess.png")

    def label(self, question: Question, choice: int) -> str:
        label = question.choices[choice]
        return geoguess_view.country_label(label) if self.name == "geoguess" else label

    def render(self, state: RoundState, votes: list[Vote]) -> View:
        question = state.question
        if question is None:
            raise ValueError("Missing quiz question")
        closed = state.phase == "closed"
        scored = None if state.score_status == "pending" else state.score_status == "recorded"
        if self.name == "chess":
            chess_players = [
                chess_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
                for vote in votes
            ]
            return chess_view.render(chess_puzzle(question), chess_players, closed=closed, scored=scored, page=state.page)
        geo_players = [
            geoguess_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
            for vote in votes
        ]
        return geoguess_view.render(geoguess_photo(question), geo_players, closed=closed, scored=scored, page=state.page)


DEFINITIONS = {name: Definition(name) for name in ("chess", "geoguess")}
