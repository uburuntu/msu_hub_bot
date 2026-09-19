# Community tools

The Mini App opens in the chat and topic from its signed `/app` link. Open it
again from another chat to change context. Telegram init data identifies the
person; it does not authorize access to a chat. Shared views verify current
membership, and settings/repost mutations require current administrator rights
for both the person and the bot. No request can supply a different destination
or topic. Personal timezone preferences are owner-scoped and permanent.

## Repost configuration

VK targets use the existing permanent `vk/subscriptions` feature collection in
application scope. Version 2 adds topic, title, keyword filters, creator metadata
and archive state. The pure upgrade preserves original IDs, creation times,
flags, description, cursor and unknown fields. Original keys remain
`owner_id:chat_id`; topic targets append `:topic:thread_id`.

Automatic publication is disabled. New targets and changes remain paused;
archiving preserves the configuration and cursor. `/vk_wall` saves a paused
configuration; `/vk_post` remains an explicit one-post action. The Mini App
cannot resume publication or rewind a cursor. Source and destination identity
are immutable: archive a target and create another when they change.

Preview reads at most three VK posts with a deadline and bounded concurrency.
It never sends a Telegram message, downloads arbitrary user URLs or advances a
cursor. Numeric wall IDs and canonical VK page links are accepted; vanity
names are resolved through the configured VK API. A post passes filters when
it matches any include term (or there are none), matches no exclude term, and
its repost flag is permitted. Comparisons are literal and case-insensitive.

Edits compare exact revisions. Creation commits the target and its permanent
request receipt atomically; a replay returns the same target and a changed
request body conflicts. Stored payloads preserve unknown fields. Neither API
responses nor telemetry expose request receipts or privileged credentials.

Before enabling publication, add a leased delivery workflow with a per-target
post identity, pre-send marker, confirmed checkpoint advancement and an
explicit hold for uncertain Telegram outcomes. A preview or an API timeout is
never evidence that a post was delivered. Do not use a source-wide timestamp
to skip failed destinations.
