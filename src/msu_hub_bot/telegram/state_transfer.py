"""Validate and restore private, normalized conversation and deletion snapshots."""

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aiogram.utils.token import validate_token
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from msu_hub_bot.storage.features import Conflict, FeatureStore, RecordKey
from msu_hub_bot.storage.features.store import Transaction
from msu_hub_bot.telegram.deletions import Deletion
from msu_hub_bot.telegram.fsm_storage import FEATURE, Conversation, FeatureFSMStorage, commit_state_change

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class PendingDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    chat_id: int = Field(strict=True)
    message_id: int = Field(strict=True, gt=0)
    run_at: AwareDatetime


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    format: Literal["feature-state-v1"]
    bot_id: int = Field(strict=True, gt=0)
    conversations: list[Conversation] = Field(max_length=100_000)
    deletions: list[PendingDeletion] = Field(max_length=100_000)

    @model_validator(mode="after")
    def unique_identities(self) -> StateSnapshot:
        if any(value.key.bot_id != self.bot_id for value in self.conversations):
            raise ValueError("Snapshot belongs to another bot")
        if len({value.key.record_key() for value in self.conversations}) != len(self.conversations):
            raise ValueError("Duplicate conversation identity")
        if len({(value.chat_id, value.message_id) for value in self.deletions}) != len(self.deletions):
            raise ValueError("Duplicate deletion identity")
        return self

    @classmethod
    def read(cls, path: Path) -> StateSnapshot:
        with path.open("rb") as source:
            data = source.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("State snapshot exceeds its size limit")
        return cls.model_validate_json(data)


@dataclass(frozen=True)
class RestoreResult:
    conversations: int
    deletions: int
    existing: int
    expired_or_empty: int


async def restore_state(store: FeatureStore, snapshot: StateSnapshot, *, bot_id: int, apply: bool = False) -> RestoreResult:
    """Require stopped writers; verify every record before committing guarded inserts.

    Each insert is atomic and repeatable. A differing existing record aborts the
    preflight; a concurrent change aborts its commit rather than overwriting it.
    Re-run an interrupted restore with the same snapshot and writers still off.
    """
    if snapshot.bot_id != bot_id:
        raise ValueError("Snapshot belongs to another bot")
    conversations = FeatureFSMStorage(store).records
    deletions = store.collection(FEATURE, "deletions", Deletion, retention=None)
    transactions: list[Transaction] = []
    existing = conversation_count = deletion_count = expired_count = 0
    for saved in snapshot.conversations:
        value = saved.model_copy(deep=True)
        value.prune_expired()
        if value.empty:
            expired_count += 1
            continue
        key, scope = value.key.record_key(), value.key.scope
        transaction = store.transaction(FEATURE, scope, operation_id=str(uuid4()))
        transaction.expect_absent(conversations.name, key)
        # Validate the complete payload, including byte limits, before any write.
        transaction.put(conversations, key, value, expires_at=value.expiry())
        record = await conversations.get(scope, key)
        if record is not None:
            current = record.value.model_copy(deep=True)
            current.prune_expired()
            if current != value or record.expires_at != value.expiry():
                raise Conflict()
            existing += 1
            continue
        transactions.append(transaction)
        conversation_count += 1
    for item in snapshot.deletions:
        deletion = Deletion(bot_id=bot_id, **item.model_dump())
        transaction = store.transaction(FEATURE, deletion.scope, operation_id=str(uuid4()))
        transaction.expect_absent(deletions.name, deletion.key)
        transaction.put(deletions, deletion.key, deletion)
        transaction.schedule(deletion.key, "delete_message", record=RecordKey(deletions.name, deletion.key), run_at=deletion.run_at)
        pending = await deletions.get(deletion.scope, deletion.key)
        if pending is not None:
            if pending.value != deletion:
                raise Conflict()
            existing += 1
            continue
        transactions.append(transaction)
        deletion_count += 1
    if apply:
        for transaction in transactions:
            await commit_state_change(transaction)
    return RestoreResult(conversation_count, deletion_count, existing, expired_count)


async def _run(path: Path, *, apply: bool) -> RestoreResult:
    # Import configured services only when explicitly running the administrator tool.
    from msu_hub_bot.settings import settings
    from msu_hub_bot.storage.factory import create_repository

    snapshot = StateSnapshot.read(path)
    settings.validate_core()
    validate_token(settings.bot_token)
    bot_id = int(settings.bot_token.partition(":")[0])
    repository = create_repository(settings)
    try:
        await repository.check()
        store = FeatureStore(repository)
        await store.check()
        return await restore_state(store, snapshot, bot_id=bot_id, apply=apply)
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore private Telegram state with every writer stopped; dry-run unless --apply.")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args.snapshot, apply=args.apply))
    except Exception:
        raise SystemExit("State restore failed validation or storage checks; no existing record was overwritten") from None
    print(
        f"{'Restored' if args.apply else 'Validated'} conversations={result.conversations} deletions={result.deletions} "
        f"existing={result.existing} expired_or_empty={result.expired_or_empty}"
    )


if __name__ == "__main__":
    main()
