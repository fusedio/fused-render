// Structural guards: window-local polls must not tick while the window is hidden
// (every window shares WebKit's 6-connection pool; see pause-while-hidden.ts).
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const read = (rel: string) => readFileSync(join(import.meta.dir, rel), "utf8");

test("the bookmark poll is gated on visibility", () => {
  const main = read("../main.tsx");
  expect(main).toContain('from "@platform/lib/pause-while-hidden"');
  expect(main).toContain("setInterval(pauseWhileHidden(pollBookmarks), BOOKMARK_POLL_MS)");
  expect(main).not.toContain("setInterval(pollBookmarks,");
});

test("the Scheduled feed poll skips hidden ticks and still catches up on return", () => {
  const src = read("Scheduled.tsx");
  expect(src).toContain('if (document.visibilityState === "hidden") return;\n      reloadFeeds();');
  expect(src).toContain("useRefreshOnReturn(reload)");
  expect(src).not.toContain("window.setInterval(reloadFeeds, POLL_MS)");
});

test("Home's useAsyncResource gates its poll and cancels the parked catch-up", () => {
  const data = read("home/data.ts");
  expect(data).toContain("const gated = pauseWhileHidden(bump);");
  expect(data).toContain("setInterval(gated, pollMs)");
  expect(data).toContain("gated.cancel();");
});

test("scheduleEvents stays ungated: it delivers events a person must hear while away", () => {
  expect(read("../platform/lib/scheduleEvents.ts")).not.toContain("pauseWhileHidden");
});
