# OpenIntelligentUI adaptation

Upstream: https://github.com/CopilotKit/OpenIntelligentUI

The MIT-licensed chat layout, reader-owned scroll approach, answer block reveal utility and touch-friendly message actions are adapted into the existing Hermes dashboard plugin. The original CSS and TypeScript utility are retained here with their license. Hermes owns transport, provider settings, sessions, recovery, uploads and tool execution. No extra Next.js or Python agent server is added.

This is an interface adaptation, not the full CopilotKit/AG-UI stack. OpenIntelligentUI's Jev visualization router, A2UI and generated interactive tool renderer are not connected. Ordinary Hermes Markdown, tables, code, attachments and tool events continue to work. No OpenAI or Jev keys are required by this adaptation.

Streaming does not move the reader unless Follow reply is enabled. The original Auto-scroll setting remains an additional off switch. Latest messages jumps to the newest reply. Completed Markdown blocks reveal on entering the viewport, with immediate display when IntersectionObserver is unavailable or reduced motion is enabled. Live text is never hidden waiting for an animation.

UI changes are scoped to .hcd. Dashboard settings, Telegram sessions and private state format are retained. The extension adds no package installs, build step or external fonts.
