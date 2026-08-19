// Self-fix's pure parts (SPEC §43). Everything here is a decision that is
// wrong in a way no screenshot shows: a handoff URL that drops the run id
// lands the user in an empty chat beside a session that is already working, an
// issue URL that forgets the version is a bug report nobody can act on, and a
// poll cadence that never leaves its fast lane is a request every five seconds
// for the life of the app.
import { expect, test } from "bun:test";

// fixSessionUrl pulls urlForFsPath from router.ts, which reads `location` at
// MODULE scope (IS_EMBED). Bun's runtime has no DOM and a static import is
// hoisted above any shim, so the stub goes first and the module comes in
// dynamically after it — the same dance as appEntry.test.ts.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};

// `noteFixStarted` writes localStorage and dispatches on `window`; neither
// exists in bun's runtime. Minimal stand-ins, shared like the two above.
const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage ??= {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
};
(globalThis as { window?: unknown }).window ??= { dispatchEvent: () => true };

// Record what `noteFixStarted` dispatches, by swapping the method IN THE TEST
// rather than by owning the `window` stub: bun shares globals across suites and
// several set `window` themselves (one of them unconditionally), so which
// object is live here depends on file order. Swapping the method works whichever
// one won.
function captureDispatch<T>(body: () => T): { result: T; types: string[] } {
  const win = globalThis.window as unknown as { dispatchEvent: (e: unknown) => unknown };
  const real = win.dispatchEvent;
  const types: string[] = [];
  win.dispatchEvent = (e: unknown) => types.push((e as Event).type);
  try {
    return { result: body(), types };
  } finally {
    win.dispatchEvent = real;
  }
}

const {
  failureContextFromJob,
  fixSessionUrl,
  issueUrl,
  lastFixStartedAt,
  mayPaint,
  maySchedule,
  modifiedSummary,
  isSelfFixStorageKey,
  noteFixStarted,
  notifySelfFixChanged,
  overtakeReads,
  restartChain,
  selffixPollInterval,
  shouldNoteStart,
  unlistedReports,
  POLL_GEN_START,
  POLL_IDLE_MS,
  POLL_WATCH_MS,
  SELFFIX_CHANGED_EVENT,
  SELFFIX_CHANGED_KEY,
  SELFFIX_PING_KEY,
  WATCH_WINDOW_MS,
} = await import("./selffix");

type Snapshot = Parameters<typeof issueUrl>[0];
type Marker = Parameters<typeof modifiedSummary>[0];

const marker = (over: Partial<Marker> = {}): Marker => ({
  modified: true,
  version: "0.4.18",
  install_root: "/Applications/FusedRender.app/Contents/Resources/lib/python3.12/fused_render",
  state_dir: "/i/.fused-render-selffix",
  first_modified_at: 1_700_000_000,
  modified_at: 1_700_000_000,
  fixes: [],
  latest_report: null,
  ...over,
});

const snapshot = (over: Partial<Snapshot> = {}): Snapshot => ({
  modified: true,
  version: "0.4.18",
  install_root: "/opt/fused_render",
  writable: true,
  marker: marker({ latest_report: "/opt/fused_render/.x/reports/r.md" }),
  reports: [],
  reinstall: { method: "dmg", headline: "h", command: "", note: "n", url: "u" },
  issues_url: "https://github.com/fusedio/fused-render/issues/new",
  machine: { platform: "macOS-15.0", python: "3.12.3" },
  ...over,
});

// -- the handoff --------------------------------------------------------------

test("the session url opens the install folder with the chat sidebar on the run", () => {
  const url = fixSessionUrl({ target: "/opt/fused_render", run_id: "run-7" });
  expect(url).toBe("/explorer/view/opt/fused_render?_side=claude&run=run-7");
});

test("a trailing separator does not become an empty path segment", () => {
  // Comes off a server path join; an empty segment would encode as "//" and
  // the codec's own filter would then silently drop it — same value, two
  // spellings, and the recents/bookmark stores would hold both.
  expect(fixSessionUrl({ target: "/opt/fused_render/", run_id: "r" })).toBe(
    "/explorer/view/opt/fused_render?_side=claude&run=r"
  );
});

test("a windows install path is normalised the way every other fs url is", () => {
  const url = fixSessionUrl({
    target: "C:\\Program Files\\fused-render\\fused_render",
    run_id: "r",
  });
  expect(url).toBe(
    "/explorer/view/C%3A/Program%20Files/fused-render/fused_render?_side=claude&run=r"
  );
});

test("the run id is escaped rather than pasted into the query", () => {
  const url = fixSessionUrl({ target: "/o", run_id: "a b&_side=git" });
  expect(url).toContain("run=a+b%26_side%3Dgit");
  // ...and the mode it would otherwise have overridden is still ours.
  expect(url.match(/_side=/g)).toHaveLength(1);
});

// -- what the session is told -------------------------------------------------

test("a failed row becomes the session's brief, attribution included", () => {
  const context = failureContextFromJob({
    id: "sys:ai-model:flux",
    title: "FLUX.2-klein-4B",
    detail: "transformer.gguf",
    kind: "download",
    state: "error",
    message: "OSError(28): No space left on device",
    page: "/models.html",
  });
  expect(context).toEqual({
    job_id: "sys:ai-model:flux",
    title: "FLUX.2-klein-4B",
    detail: "transformer.gguf",
    kind: "download",
    state: "error",
    message: "OSError(28): No space left on device",
    page: "/models.html",
    source: "download manager",
  });
});

// -- telling the developers ---------------------------------------------------

test("the issue url carries the version, the platform and where the report is", () => {
  const url = new URL(issueUrl(snapshot()));
  expect(url.origin + url.pathname).toBe(
    "https://github.com/fusedio/fused-render/issues/new"
  );
  expect(url.searchParams.get("title")).toBe("Self-fix report — v0.4.18");
  const body = url.searchParams.get("body") ?? "";
  expect(body).toContain("/opt/fused_render/.x/reports/r.md");
  expect(body).toContain("v0.4.18");
  expect(body).toContain("macOS-15.0");
});

test("the report itself is never inlined into the issue url", () => {
  // A report is a document — thousands of words — and a URL that long is
  // refused long before it reaches GitHub. The panel copies it to the
  // clipboard instead; the URL carries only what the user would otherwise be
  // asked for twice.
  const url = issueUrl(snapshot());
  expect(url.length).toBeLessThan(1500);
});

test("an issue can still be filed when no report was written", () => {
  const url = new URL(issueUrl(snapshot({ marker: marker(), reports: [] })));
  expect(url.searchParams.get("body")).toContain("(none written)");
});

// -- the summary line ---------------------------------------------------------

test("the summary says when, because that is the first thing anyone asks", () => {
  const now = 1_700_000_000_000;
  expect(modifiedSummary(marker({ modified_at: now / 1000 }), now)).toContain("today");
  expect(
    modifiedSummary(marker({ modified_at: now / 1000 - 86400 }), now)
  ).toContain("yesterday");
  expect(
    modifiedSummary(marker({ modified_at: now / 1000 - 5 * 86400 }), now)
  ).toContain("5 days ago");
});

test("more than one session is counted, not collapsed", () => {
  const fixes = [
    { at: 1, updated_at: 1, run_id: "a", session_id: "", title: "", report: null, incident: null },
    { at: 2, updated_at: 2, run_id: "b", session_id: "", title: "", report: null, incident: null },
  ];
  expect(modifiedSummary(marker({ fixes }))).toContain("2 fix sessions");
});

// -- the two-speed poll -------------------------------------------------------

test("no fix has ever been started here: the slow lane", () => {
  expect(selffixPollInterval(0)).toBe(POLL_IDLE_MS);
});

test("a fix just started: fast enough to see the badge appear", () => {
  const now = 1_700_000_000_000;
  expect(selffixPollInterval(now - 1000, now)).toBe(POLL_WATCH_MS);
});

test("the fast lane expires, so a stale stamp cannot poll forever", () => {
  const now = 1_700_000_000_000;
  expect(selffixPollInterval(now - WATCH_WINDOW_MS - 1, now)).toBe(POLL_IDLE_MS);
});

// A started fix has to reach a chip in THIS document as well as in other tabs,
// and the two channels are exclusive: `storage` fires everywhere except the
// document that wrote the value. Missing the same-document half is not a corner
// case — the download manager that starts the fix is normally in the very
// document the chip lives in, so the badge would wait out a full idle interval
// in the ordinary case, which is exactly what the fast cadence exists to avoid.
test("starting a fix announces it on BOTH channels", () => {
  const { types } = captureDispatch(() => noteFixStarted(1_700_000_000_000));

  expect(lastFixStartedAt()).toBe(1_700_000_000_000); // the stamp, for the cadence
  expect(types).toContain(SELFFIX_CHANGED_EVENT); // same-document nudge
});

test("a dismiss is a state change too, on the same two channels", () => {
  // The Preferences tab and the chip's own popover can both dismiss. Without a
  // nudge, dismissing from Preferences cleared the marker and refreshed that
  // page while the sidebar chip stayed amber for a full idle interval — which
  // reads as a dismiss that did not work.
  const { types } = captureDispatch(() => notifySelfFixChanged(1_700_000_000_000));
  expect(types).toContain(SELFFIX_CHANGED_EVENT);
  expect(localStorage.getItem(SELFFIX_CHANGED_KEY)).toBe("1700000000000");
});

test("a dismiss does NOT move the fast-poll stamp", () => {
  // Cadence and "re-read now" are different questions. Reusing the start stamp
  // for a dismiss would put every tab into the 5s lane for half an hour for a
  // state change that is already over.
  noteFixStarted(1_600_000_000_000);
  notifySelfFixChanged(1_900_000_000_000);
  expect(lastFixStartedAt()).toBe(1_600_000_000_000);
});

test("the storage guard accepts both of our keys and nothing else", () => {
  // A guard that let only one through would silently swallow a whole channel.
  expect(isSelfFixStorageKey(SELFFIX_PING_KEY)).toBe(true);
  expect(isSelfFixStorageKey(SELFFIX_CHANGED_KEY)).toBe(true);
  expect(isSelfFixStorageKey("fused-render:jobs-ping")).toBe(false);
  expect(isSelfFixStorageKey(null)).toBe(false);
});

test("the stamp is written before the same-document event fires", () => {
  // A listener re-reads lastFixStartedAt() to pick its cadence; dispatching
  // first would hand it the PREVIOUS stamp and leave it in the slow lane.
  let seen = -1;
  const win = globalThis.window as unknown as { dispatchEvent: (e: unknown) => unknown };
  const real = win.dispatchEvent;
  win.dispatchEvent = () => (seen = lastFixStartedAt());
  try {
    noteFixStarted(1_800_000_000_000);
  } finally {
    win.dispatchEvent = real;
  }
  expect(seen).toBe(1_800_000_000_000);
});

test("a stamp from the future reads as not watching, not as watching forever", () => {
  // A suspend or a manual clock change puts `now` behind the stamp. The safe
  // direction is the slow lane: a badge a minute late costs nothing, a
  // permanent 5s poll costs a request every 5s for the life of the app.
  const now = 1_700_000_000_000;
  expect(selffixPollInterval(now + 60_000, now)).toBe(POLL_IDLE_MS);
});

test("a nudge retires the loop that issued the read, not just the read", () => {
  // The chip holds ONE timer handle. An abandoned read that still schedules
  // overwrites the live loop's handle, so both keep polling and nothing points
  // at the orphan any more — not even the unmount cleanup. Two counters is
  // what lets the abandoned one know to stop.
  const issued = POLL_GEN_START;
  const now = restartChain(issued);
  expect(mayPaint(issued, now)).toBe(false);
  expect(maySchedule(issued, now)).toBe(false);
});

test("a fresher seed drops the answer and KEEPS the loop", () => {
  // The other half of the same rule, and the one a single epoch gets wrong:
  // nothing re-polls on this path, so retiring the loop here would leave the
  // chip never updating again for the life of the page.
  const issued = POLL_GEN_START;
  const now = overtakeReads(issued);
  expect(mayPaint(issued, now)).toBe(false);
  expect(maySchedule(issued, now)).toBe(true);
});

test("two nudges during one read leave exactly one loop standing", () => {
  // Not a corner case: `noteFixStarted` writes BOTH storage keys, so every
  // other tab re-arms twice while the first read is still open.
  const first = POLL_GEN_START;
  const second = restartChain(first); // the loop nudge #1 started
  const third = restartChain(second); // ...retired by nudge #2
  expect(maySchedule(first, third)).toBe(false);
  expect(maySchedule(second, third)).toBe(false);
  expect(maySchedule(third, third)).toBe(true);
  expect(mayPaint(third, third)).toBe(true);
});

test("a seed change under a live loop does not make the next nudge a no-op", () => {
  // `reads` moving alone must not desynchronise `chain`, or a nudge after a
  // seed change would fail to retire the loop it interrupted.
  const issued = POLL_GEN_START;
  const afterSeed = overtakeReads(issued);
  expect(maySchedule(issued, afterSeed)).toBe(true);
  const afterNudge = restartChain(afterSeed);
  expect(maySchedule(issued, afterNudge)).toBe(false);
  expect(maySchedule(afterSeed, afterNudge)).toBe(false);
});

// -- every report stays reachable ---------------------------------------------

type Fix = Marker["fixes"][number];
const fix = (report: string | null): Fix => ({
  at: 1,
  updated_at: 1,
  run_id: "r",
  session_id: "s",
  title: "t",
  report,
  incident: null,
});

const report = (name: string, at = 1_700_000_000) => ({
  path: `/i/.fused-render-selffix/reports/${name}`,
  name,
  at,
  size: 100,
});

test("a report the marker does not name is still listed while a badge is up", () => {
  // The three ways a report falls out of `marker.fixes` and the reason the
  // panel reads the DIRECTORY: the cap (MAX_FIXES), a dismiss that dropped the
  // marker but kept the files, and a session that changed nothing.
  const kept = report("old.md");
  const named = report("new.md");
  const m = marker({ fixes: [fix(named.path)] });
  expect(unlistedReports([named, kept], m).map((r) => r.name)).toEqual(["old.md"]);
});

test("with no marker at all, every report is unlisted — which is the point", () => {
  const rs = [report("a.md"), report("b.md")];
  expect(unlistedReports(rs, null)).toHaveLength(2);
  expect(unlistedReports(rs, undefined)).toHaveLength(2);
});

test("a fix that wrote no report cannot swallow one that did", () => {
  // `report` is nullable on a fix entry; a null must not match a real path, or
  // one no-op session would hide an unrelated file from the list.
  const only = report("real.md");
  const m = marker({ fixes: [fix(null)] });
  expect(unlistedReports([only], m).map((r) => r.name)).toEqual(["real.md"]);
});

test("a diagnostic start does not arm the fast poll", () => {
  // The stamp buys a 5s cadence for half an hour so a badge appears while the
  // user is still watching. A diagnostic session runs on an installation it
  // cannot write to and nothing stamps a marker for it, so the same cadence
  // buys 360 extra polls of a value guaranteed not to move (SF-13).
  expect(shouldNoteStart({ run_id: "r", target: "/x", incident: "/i", report: "/r" })).toBe(true);
  expect(
    shouldNoteStart({ run_id: "r", target: "/x", incident: "/i", report: "/r", diagnostic: true })
  ).toBe(false);
});
