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

The complete report is private and available to the configured bot owner in the
Mini App's **Отзывы** section. The administrator chat receives a bounded description,
author identity and a link to the report; it does not receive the selected context.
Both destinations are explained before submission.

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

Versioned documents use the existing `feedback` feature and bot-owned `inbox`
scope. All user-facing draft operations explicitly check the author and bound
Telegram card. Drafts have a fixed 24-hour lifetime; creating another draft
replaces the author's active one.
Up to five submissions per author are accepted in a rolling hour, and up to 50
new drafts in 24 hours. Canceled or superseded triggers cannot reopen a draft
within that window; submitted triggers remain deduplicated permanently.

Submission atomically removes the draft, writes the selected report and review
index, and schedules its notification. Submitted reports, including the explicitly
selected snapshots, are retained permanently for investigation. This is stated before submission and is
separate from the general message archive's 30-day retention. Unselected candidate
context is discarded. Do not paste credentials into a report.

Notifications use one bounded text message with a review link when the Mini App
is configured. Long descriptions are shortened only in this notification;
the submitted report stays complete. The destination is frozen before consent;
delivery never silently follows a later configuration change. Confirmed Telegram
message IDs are recorded. Explicit rate-limit rejection can be retried; a lost
response or interrupted send is marked uncertain to avoid automatic duplicates.
Queued, failed and uncertain notifications do not hide the saved report from review.

## Review in the Mini App

Only `HUB_OWNER_ID` can list, read or update reports. Every API request verifies
Telegram init data and checks that identity; chat administration, report authorship
and possession of a link grant no review access. The **Отзывы** navigation item
uses the same capability. Notifications open a private bot chat, then the selected
report in the Mini App; the report ID in the link is not a credential.

The inbox lists newest submissions first, with category/status filters and cursor
pagination. Reviewers can move a report through new, in progress, done or dismissed
and save a private note. The page shows only the frozen, selected context and the
notification's delivery state. Review notes stay in private storage.

Review metadata uses a separate `review_index` record: a short immutable summary
and author name, status, note, reviewer and time. Its sortable key supports bounded
lists without loading report bodies. The full report stores this index key.
Review updates require the index's current etag; stale edits receive a conflict,
with the unsaved note preserved for comparison. Delivery uses a separate report
revision, so reviewing a report cannot disrupt its send receipt. Browser drafts
remain in memory while switching tools or reports, and are not persisted locally.

## Configuration and operations

`HUB_EVENTS_CHAT_ID` is the notification destination. When unset, the command
gives an unavailable response. Use the actual Telegram chat ID and ensure the bot
can send messages there. `HUB_OWNER_ID` grants review access; zero grants none.
`HUB_WEB_APP_URL` enables the review UI and notification links. Destination IDs
and reviewer identities belong in private configuration, not documentation.

Apply `010_feedback_context.sql` using [database operations](database-operations.md)
before enabling archived context. It adds a principal-gated reader over existing
messages and creates no table. Review the installed retention/readiness revision
guards when applying it. If the reader is unavailable, the report still works
with that limitation shown; it never silently substitutes context from another chat.

For investigation, use the report ID in the `feedback` feature's `inbox` scope.
Inspect the `reports` collection and its notification state before retrying manually;
an uncertain send might already exist in the administrator chat. Preserve the feature
store's exact revision guards and frozen operation retry rules.
