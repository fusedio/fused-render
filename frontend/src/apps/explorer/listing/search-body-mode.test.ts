import { describe, expect, test } from "bun:test";
import { showingSearchHits } from "@apps/explorer/listing/search-body-mode";
import type { SearchState } from "@apps/explorer/listing/types";

const OK: SearchState = { status: "ok", truncated: false, total: 1, forRefresh: 0, elapsedMs: 1 };
const PENDING: SearchState = { status: "pending", forRefresh: 0 };
const ERROR: SearchState = { status: "error", message: "boom", forRefresh: 0 };
const IDLE: SearchState = { status: "idle" };

describe("showingSearchHits", () => {
  test("not searching at all (idle) never shows hits — the folder's own rows render", () => {
    expect(showingSearchHits(IDLE, false)).toBe(false);
  });

  test("a typed-but-uncommitted query with no answer ever fetched (idle) shows the folder", () => {
    // Ctrl/Cmd+L lands here: seeded, escaping, uncommitted, nothing ever asked.
    expect(showingSearchHits(IDLE, false)).toBe(false);
  });

  test("a settled answer with rows, committed, shows the hits", () => {
    expect(showingSearchHits(OK, false)).toBe(true);
  });

  test("awaitingCommit wins even when a previous committed answer is 'ok' on screen", () => {
    // An escaping edit typed over a committed search that had returned rows:
    // status is still "ok" for the stale answer, but it is not what Enter
    // will act on, so the folder's own rows show instead.
    expect(showingSearchHits(OK, true)).toBe(false);
  });

  test("pending and error both show as search hits (their own status rows)", () => {
    expect(showingSearchHits(PENDING, false)).toBe(true);
    expect(showingSearchHits(ERROR, false)).toBe(true);
  });
});
