# Feedback

`/feedback Add chess leaderboard` or `/feedback Last few commands failed` opens
an editable report card. A reply can identify the message involved. The command
preserves the author's message and does not start a conversation; active flows
keep their existing `/cancel` behavior.

Choose bug, idea or other, select the context, preview the report, then submit.
Changing any selection invalidates the previous preview. Only the author can
use the card; copied or stale buttons cannot submit a different snapshot.
A failed preview delivery does not authorize submission.

## What gets shared

The report always contains its description, category, author identity, creation
time and report ID. The card separately offers:

| Choice | Included information | Default |
| --- | --- | --- |
| Chat | Chat label, identifier and forum topic | On |
| Replied message | Bounded text/caption, author, message ID, timestamp and media kind | On when available |
| Recent messages | Up to five earlier messages from the same chat/topic | Off |
| Command diagnostics | Up to five recent commands by the reporter in this chat/topic | On when available |

Snapshots do not download media or copy Telegram's complete update JSON.
Unavailable context is identified in the preview. Excerpts are explicitly marked
when shortened. The preview is the complete submitted report, provided as a UTF-8
text file when it exceeds a Telegram message. Ordinary IDs remain intact.

Chat identifiers can also be present in selected message snapshots. Turning off
the chat checkbox removes the separate chat section, not attribution needed to
identify a message the user has explicitly selected.

The archive is best-effort history: it includes messages observed by the bot and
can still include messages subsequently deleted in Telegram. Sources older than
30 days, other forum topics, business-message contexts and channel direct-message
topics are excluded. The current replied-to message is captured directly from the
incoming update; it need not wait for archival persistence.

Command diagnostics remain local to the bot process, expire after 30 minutes,
and have a global capacity limit. They contain identifiers, command/handler names,
times and outcomes; no arguments, message bodies or exception strings. A handler
that returned normally is labelled completed, which does not prove that an
external provider succeeded. Restarts clear this history. No extra Logfire exports
or remote log searches are needed to create a report.

## Persistence and delivery

Versioned documents use the existing `feedback` feature. Drafts have a fixed
24-hour lifetime; creating another draft replaces the author's active one.
Up to five submissions per author are accepted in a rolling hour, and up to 50
new drafts in 24 hours. Canceled or superseded triggers cannot reopen a draft
within that window; submitted triggers remain deduplicated permanently.

Submission atomically removes the draft, writes the selected report and schedules
delivery. Submitted reports, including the explicitly selected snapshots, are
retained permanently for investigation. This is stated before submission and is
separate from the general message archive's 30-day retention. Unselected candidate
context is discarded. Do not paste credentials into a report.

Short reports are delivered as one text message. Longer reports use one complete
text document with a concise caption. The destination is frozen before consent;
delivery never silently follows a later configuration change. Confirmed Telegram
message IDs are recorded. Explicit rate-limit rejection can be retried; a lost
response or interrupted send is marked uncertain to avoid automatic duplicates.
Queued, failed and uncertain reports remain available in the feature store.

## Configuration and operations

`HUB_FEEDBACK_CHAT_ID` overrides the review destination; zero uses
`HUB_EVENTS_CHAT_ID`. With both unset, the command gives an unavailable response.
`HUB_FEEDBACK_DESTINATION_NAME` controls the name displayed before submission.
Use the actual Telegram chat ID and ensure the bot can send messages and documents
there. Destination IDs belong in private configuration, not documentation.

Apply `010_feedback_context.sql` using [database operations](database-operations.md)
before enabling archived context. It adds a principal-gated reader over existing
messages and creates no table. Review the installed retention/readiness revision
guards when applying it. If the reader is unavailable, the report still works
with that limitation shown; it never silently substitutes context from another chat.

For investigation, use the report ID and reporter's feature scope `user:<id>`.
Inspect the `reports` collection and its delivery state before retrying manually;
an uncertain send might already exist in the review chat. Preserve the feature
store's exact revision guards and frozen operation retry rules.
