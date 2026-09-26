"""Credential-free application definition for inspect and offline tests."""

from aiogram.utils.formatting import Bold, Text

from teleforge import (
    App,
    Argument,
    Button,
    CallbackContext,
    Card,
    Context,
    Feature,
    MessageContext,
    action,
    card,
    command,
    show,
)


class Counter(Feature, key="counter"):
    def __init__(self) -> None:
        # Deliberately ephemeral: durable scores belong in an application repository.
        self.scores: dict[int, int] = {}

    @command("hello", count=Argument(clamp=(1, 5)))
    async def hello(self, count: int = 1) -> Text:
        return Text(*[Text("Hello, ", Bold("Telegram"), "!\n") for _ in range(count)])

    @command("score")
    async def score(self, ctx: MessageContext) -> None:
        await show(ctx, self.board)

    @card
    async def board(self, ctx: Context) -> Card:
        return Card(Text("Together: ", Bold(sum(self.scores.values()))), buttons=[[Button("+1", self.add)]])

    @action(key="increment", card="board")
    async def add(self, ctx: CallbackContext) -> None:
        assert ctx.user is not None
        self.scores[ctx.user.id] = self.scores.get(ctx.user.id, 0) + 1
        await ctx.answer("Counted!")


def make_app() -> App:
    return App().include(Counter())
