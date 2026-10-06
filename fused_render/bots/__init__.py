"""Browser Bots: the bot backend that lives inside the fused-render server
(docs/bots.md). Nothing here is imported at server start except
`routes` and `registry`; the heavy modules load on first use."""
