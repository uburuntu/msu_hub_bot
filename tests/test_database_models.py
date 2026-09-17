from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from common.db.models import ArchivedUpdate, ChatObservation, ChatRecord, DirectoryPatch, UserRecord, VkPatch


@pytest.mark.parametrize("metadata", [{"future": [None, "Тест"]}, "{}", [], None, 7])
def test_records_preserve_legacy_json_shapes_and_source_identity(metadata):
    row = ChatRecord(id=UUID(int=1), created="2019-01-02T03:04:05.123456Z", chat_id=-(2**52), type="group", metadata=metadata)
    assert row.metadata == metadata
    assert row.id == UUID(int=1)
    assert row.created == datetime(2019, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert row.chat_id == -(2**52)


def test_sparse_patches_and_observations_distinguish_absence_from_clear():
    assert VkPatch().model_dump(exclude_unset=True) == {}
    assert VkPatch(description=None).model_dump(exclude_unset=True) == {"description": None}
    assert DirectoryPatch(pinned_message_id=None).model_dump(exclude_unset=True) == {"pinned_message_id": None}
    sparse = ChatObservation(chat_id=1, type="private")
    cleared = ChatObservation(chat_id=1, type="private", username=None)
    assert "username" not in sparse.model_dump(exclude_unset=True)
    assert cleared.model_dump(exclude_unset=True)["username"] is None


@pytest.mark.parametrize(
    "patch", [lambda: VkPatch(last_post_id=None), lambda: VkPatch(with_header=None), lambda: DirectoryPatch(name=None)]
)
def test_required_fields_cannot_be_cleared(patch):
    with pytest.raises(ValidationError):
        patch()


@pytest.mark.parametrize("identity", [True, 1.0, "12", 2**63, -(2**63) - 1])
def test_ids_cannot_be_rounded_or_coerced(identity):
    with pytest.raises(ValidationError):
        ChatObservation(chat_id=identity, type="group")


def test_dates_must_identify_an_instant_and_archive_has_its_own_stable_id():
    with pytest.raises(ValidationError):
        ChatObservation(chat_id=1, type="private", observed_at="2026-01-02T03:04:05")
    update = ArchivedUpdate(update_id=1, kind="message", handled=True, data={})
    assert ArchivedUpdate.model_validate_json(update.model_dump_json()).id == update.id
    assert update.id != ArchivedUpdate(update_id=1, kind="message", handled=True, data={}).id
    assert update.received_at.utcoffset().total_seconds() == 0


def test_computed_names_preserve_empty_and_missing_source_values():
    base = dict(id=UUID(int=1), created=datetime.now(UTC), metadata={})
    user = UserRecord(**base, user_id=1, is_bot=False, first_name="First", last_name="")
    assert user.full_name == "First "
    assert ChatRecord(**base, chat_id=1, type="group", title="", first_name="Ignored").full_name == ""
    assert ChatRecord(**base, chat_id=1, type="group").full_name is None


def test_database_validation_does_not_echo_sensitive_values():
    marker = "private-invalid-marker"
    with pytest.raises(ValidationError) as error:
        ChatObservation(chat_id=marker, type="private")
    assert marker not in str(error.value)
