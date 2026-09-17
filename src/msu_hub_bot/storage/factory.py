"""Select one persistence backend without applying schema or data changes."""

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.edgedb import EdgeDBRepository
from msu_hub_bot.storage.supabase import SupabaseRepository
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Telemetry


def create_repository(config: Settings, *, telemetry: Telemetry | None = None) -> BotRepository:
    if config.storage_backend == "edgedb":
        return EdgeDBRepository(config=config)
    if config.storage_backend == "supabase":
        return SupabaseRepository(config, telemetry=telemetry)
    raise ValueError("Unsupported persistence backend")
