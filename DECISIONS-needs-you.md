# Needs-you visibility: decisions

## A. Unanswered questions and approvals
- `permission_server.py` `_unanswered()` appends `[fused:unanswered reason=<timeout|expired|abandoned>(  minutes=N)]` to the
  deny message for no-human outcomes. The human sentence is kept. `cancelled` and a human's own deny carry no marker.
- `protocol/unanswered.ts` matches the marker on a tool segment's `output` (both transcript shapes carry it: it is the
  tool_result text). `Turn.tsx` draws `.turn-unanswered` under the body whether the turn is folded or open, so no fold
  exemption was needed; the notice sits outside the fold branch. For AskUserQuestion it lists the question texts; for
  other tools, `toolChipSummary`.
- Tests were written alongside the implementation for the frontend half (protocol test and Turn test passed on first run);
  the Python test was watched failing first.

## B. API error rows: the brief was wrong, already built
- agent.py `_history` already lifts `isApiErrorMessage` rows into `role: "error"` turns (`_is_api_error_row`), history.ts
  maps them, Turn.tsx draws them red and unfolded; the live path uses `reportTrouble`. Nothing dropped them.
- Added a regression test (tests/test_claude_history_errors.py) for the exact incident sequence: AskUserQuestion,
  timeout tool_result, limit row. No "Stopped:" prefix was added: the red row plus trouble card already carry the text.

## C. Workspace card caption
- `attentionCaption` now also returns "Run failed" for `blocked` + `blocked_reason: "failed"`, so List/Board/Cards gain it
  through their existing `asking` seat. New `needsYouCaption` = attentionCaption || usageLimitCaption, used by the
  Workspace card.
- Deviation: usage limit keeps the existing `usageLimitCaption` wording ("Usage limit · resumes 3:00 AM"), not
  "Paused · usage limit", because List/Board/Cards and the chat header already say it and one vocabulary is the point.
  List/Board/Cards already drew the usage-limit caption.
- Workspace card: the caption takes the preview's seat (`.ws-preview.ws-ask`), replacing the last-message preview on
  Needs-you cards. Drafts get no caption (their preview stays).
