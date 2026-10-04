// THE ONE route -> label table for "who raised this notification" — a
// closed set of shell SPA routes a few server producers name directly
// (never an fs path). Used from THREE places that used to each hold their
// own copy of this knowledge:
//   - `router.ts`'s `JOB_PAGE_ROUTES` (a bare Set, no labels) now derives
//     its membership from this table's keys instead of repeating the list.
//   - `format.ts`'s `labelForSource` consults this table first, exactly as
//     the server's `origin_for_page` does, before falling back to a bare
//     basename for an fs path.
//   - `fused_render/jobs.py`'s `_ORIGIN_BY_ROUTE` is the SAME table,
//     mirrored server-side (Python can't import TS). Kept in sync by hand
//     and CHECKED by `tests/test_jobs_api.py`'s
//     `test_origin_by_route_matches_the_client_table`, which parses this
//     file's object literal and diffs it against the Python dict — a route
//     added on either side without the matching entry on the other fails
//     that test, not just a `bun test` run.
//
// LEAF MODULE: no imports, no module-scope side effects — the same
// property `format.ts` itself has, and for the same reason. `router.ts`
// reads `location` at module scope, so importing router.ts (even
// transitively, via `notifications.ts`) from `shell/tasks-lib.ts` broke
// `tasks-lib.test.ts` (`ReferenceError: location is not defined`) the one
// time a prior fix tried it. This table lives below that fault line so
// every consumer — including `router.ts` itself — can import it with no
// risk of dragging a DOM dependency in.
//
// A key that itself carries a query string (`"/preferences?tab=indexing"`)
// is a MORE SPECIFIC entry that must be tried before the bare route
// (`"/preferences"`) is — see `labelForSource`'s own lookup order.
export const ORIGIN_BY_ROUTE: Readonly<Record<string, string>> = {
  "/ai-models/local": "Local models",
  "/ai-models/benchmark": "Benchmark",
  "/claude-config": "Claude setup",
  "/preferences": "Preferences",
  "/preferences?tab=indexing": "Explorer",
  "/tasks": "Scheduler",
};
