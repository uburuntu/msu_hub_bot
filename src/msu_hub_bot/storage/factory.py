"""Compose application storage without applying schema or data changes."""

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.supabase import SupabaseRepository
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Telemetry


def create_repository(config: Settings, *, telemetry: Telemetry | None = None) -> BotRepository:
    return SupabaseRepository(config, telemetry=telemetry)
