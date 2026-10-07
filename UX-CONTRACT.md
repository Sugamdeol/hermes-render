# Chat behavior

Canonical owners: hermes-chat-dashboard Composer, MessageView, SessionSidebar,
ActivityPanel and InfoPanel. Existing Gateway client and plugin backend own
session lifecycle; upstream host owns dashboard navigation and auth.
Source: dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py and
its bundle/index.js; pinned tui_gateway server prompt.submit/session.interrupt.

Enter sends when enabled; Shift+Enter adds a line. IME Enter never submits.
Composer blocks duplicate submissions, read-only messages and pending uploads.
Failed send restores its draft through the existing send handler. Copy feedback
waits for clipboard success. Focus hides both side panels; Details and history
can be reopened using named buttons. Latest messages scrolls only on request
when the reader has moved away from the bottom. Existing settings remain the
source of truth for density, model, toolsets and history. Native select owns
mode popup geometry. No new agent connection or background polling is added.

History is read in bounded SQL pages ordered by timestamp and ID, with an exact
count. Loading older history is explicit and single-flight; page errors are
visible and retryable. New selection invalidates old fetches and resumes.
Completed message rendering is memoized. Stream text is escaped and batched,
then rendered as markdown on completion. Model discovery is independent of
history boot. Reconnect invalidates the old gateway session handle; a send
resumes the stored conversation before submitting. No prompt is replayed.

Model changes target the live conversation. Provider-qualified picker IDs are translated into Hermes' explicit provider flag, including model names containing colons. A selection during generation is queued for the next submitted reply; the active model is only changed after gateway acknowledgement. Failed queued switches preserve the draft and block submission so it cannot silently use the wrong model. Navigating to another conversation clears the pending choice.

Live deltas create a reply even when the start event is lost. Live replies bypass offscreen content skipping. After 12 seconds without reply events, the active accepted turn receives a small status check every five seconds; when complete, one bounded history page reconciles the transcript. No prompt is replayed and idle chats do not poll. The sidebar includes native compression tips, supports source filtering and explicit older-session paging.

## Audit fixes — 1.4.2

New conversations resolve the persistent database session ID after prompt acceptance. Reconnecting interrupts the displayed streaming state rather than leaving the composer stuck. Secret and sudo inputs are masked. Markdown protects links and inline code before formatting; very large code blocks skip syntax highlighting. Uploads and UI metadata use private atomic temporary files, and damaged metadata fails visibly instead of being replaced with empty defaults. Shared transcript reads are bounded and blocking database endpoints run outside the event loop.

## Refresh during a reply — 1.5.0

A browser disconnect detaches the client, not the isolated agent worker. The latest partial assistant text and reasoning are bounded snapshots. History reads include this snapshot; session.resume reattaches to an existing live session instead of creating a new turn. The UI merges the latest snapshot before accepting further deltas, remembers the selected conversation and restores it on a page refresh. RPC request IDs are remapped so replies for a disconnected browser cannot complete a newer browser's request. Idle disconnected workers are reaped after two minutes once no turn is running. Dashboard shutdown terminates the worker group. This covers browser refresh/reconnect; snapshots are in memory and are not a guarantee against whole-host loss.
