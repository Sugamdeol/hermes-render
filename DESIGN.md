# Hermes chat design

## Overview
Conversation-first research and coding workspace for Sugam. Preserve the host dark teal theme; use readable system sans instead of its display font. Keep side panels optional and the conversation central. No new server processes or remote fonts.

## Colors
The runtime token owner is dashboard-plugins/hermes-chat-dashboard/dashboard/bundle/style.css `.hcd` variables, adapted from the host --color-* theme. This document does not duplicate host color values. Theme changes retain semantic text, warning and error roles.

## Typography
--hcd-ui-font is system sans for chat controls and prose; ui-monospace is reserved for code. Composer 16px, message body 15px/1.75, utility labels 11–13px. Existing small/large message preferences remain available.

## Layout
--hcd-reading-width 840px bounds transcript/composer. History is 248px and details 280px. The history starts with a restrained Hermes monogram and workspace label. User messages have a soft tinted surface; agent responses stay on the transcript canvas. Details start closed; history starts closed on narrow screens. Focus hides both panels. Container queries respond to available chat width, not just browser width.

## Elevation & Depth
Only the composer and floating overlays receive shadows. No backdrop blur, animated backgrounds, remote fonts or new rendering dependencies. Runtime --hcd-composer-shadow owns composer elevation.

## Shapes
Rounded workspace shell (22px), composer (22px), controls (9–13px). The square top-left corner on user bubbles separates authors at a glance. Runtime CSS owns geometry.

## Components
The existing plugin owns message rendering, composer, command palette, tool activity, metadata and settings. Extend those owners rather than introducing another chat or gateway connection. CSS overrides are scoped to `.hcd`; other dashboard routes retain their typography.

## Do's and Don'ts
Quick 140ms color/border feedback only; do not animate message height or replay entrance animations while streaming. Starting prompts are buttons that fill the draft without sending. Keep keyboard focus visible and motion optional. Keep code copy/export, model selection, attachments and tools available. Never replay a prompt automatically. Submission locks prevent duplicate sends. Read-only sessions cannot submit.
