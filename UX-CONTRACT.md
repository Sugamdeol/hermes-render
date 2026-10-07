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
