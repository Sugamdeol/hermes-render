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
