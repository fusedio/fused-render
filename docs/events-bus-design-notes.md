# Fused Events Bus: implementation notes

Placeholder for the implementation plan of the Fused Events Bus design
(one WebSocket per document, every live fact a subscription). The design
document is the source of truth; this file will carry per-phase notes as
each phase lands.

Phases, in PR order:

0. the bus (`server/events.py`, `/api/events`, `static/events-client.js`)
1. shell ambient polls
2. page side (`fused.subscribe`)
3. the streams (`claude.run`, `bots`, `claude.live`)
4. one-shot waits
5. removal of the long-poll endpoints
