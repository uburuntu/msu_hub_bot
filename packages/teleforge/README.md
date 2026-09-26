# TeleForge

TeleForge is an opinionated aiogram application framework built around cohesive
feature classes, explicit invocation contexts and application-owned persistence.
It supports coding agents with inspectable declarations, actionable diagnostics
and executable examples.

```python
from aiogram.utils.formatting import Bold, Text
from teleforge import App, Feature, command


class Greetings(Feature, key="greetings"):
    @command("hello")
    async def hello(self, name: str = "friend") -> Text:
        return Text("Hello, ", Bold(name), "!")


app = App(Greetings())
```

Supply an aiogram `Bot` to `app.run_polling(bot)`, or embed `app.build_router()`
in an existing dispatcher with `app.lifespan()` around its runtime. Constructor
dependencies remain ordinary Python; native filters, middleware and Telegram
types remain available.

The [counter example](examples/quickstart.py) adds a managed card with buttons
bound directly to typed methods. The [authoring guide](docs/index.md) covers
media, callbacks, conversations, delivery limits, persistence and lifecycle.
See [AGENTS.md](AGENTS.md) before contributing.
