"""Keep optional clients from blocking a minimally configured bot."""

from msu_hub_bot.settings import MissingIntegration


class UnavailableClient:
    def __init__(self, *fields: str):
        self.fields = fields

    async def close(self):
        pass

    def __getattr__(self, name):
        async def unavailable(*args, **kwargs):
            raise MissingIntegration(*self.fields)

        return unavailable
