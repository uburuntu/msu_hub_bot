"""Permanent personal defaults with optimistic updates shared by bot and Mini App."""

from uuid import uuid4

from pydantic import field_validator

from msu_hub_bot.reminders.models import DEFAULT_TIMEZONE, valid_zone
from msu_hub_bot.storage.application import ApplicationDocuments
from msu_hub_bot.storage.features import Conflict, FeatureStore, Payload, Record, Scope


class UserPreferences(Payload):
    timezone: str = DEFAULT_TIMEZONE

    _zone = field_validator("timezone")(valid_zone)


class Preferences:
    def __init__(self, store: FeatureStore) -> None:
        self.store = store
        self.items = store.collection("preferences", "users", UserPreferences, retention=None)

    @staticmethod
    def scope(user_id: int) -> Scope:
        if user_id <= 0:
            raise ValueError("Preferences need an authenticated user")
        return Scope(f"user:{user_id}")

    async def get(self, user_id: int) -> Record[UserPreferences] | None:
        return await self.items.get(self.scope(user_id), "defaults")

    async def timezone(self, user_id: int) -> str:
        value = await self.get(user_id)
        return value.value.timezone if value is not None else DEFAULT_TIMEZONE

    async def update(self, user_id: int, timezone: str, etag: str | None) -> Record[UserPreferences]:
        current = await self.get(user_id)
        if (current.etag if current else None) != etag:
            raise Conflict()
        value = current.value.model_copy(deep=True) if current else UserPreferences()
        value.timezone = valid_zone(timezone)
        tx = self.store.transaction("preferences", self.scope(user_id), operation_id=uuid4().hex)
        if current is None:
            tx.expect_absent("users", "defaults")
        else:
            tx.expect(current)
        tx.put(self.items, "defaults", value)
        result = await ApplicationDocuments._commit(tx)
        return self.items.decode(result.records[0], self.scope(user_id))
