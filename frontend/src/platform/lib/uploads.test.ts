// The mount upload queue's two UI decisions (D221). Both are about telling
// "verified clear" apart from "we don't know", which is the difference between
// an honest card and a false all-clear over files that never left the machine.
import { expect, test } from "bun:test";

import { hasDrainingUploads, uploadNotice } from "./uploads";
import type { MountUploads } from "./api";

const clear: MountUploads = { unknown: false, pending: 0, failed: 0, failed_names: [] };
const queued = (pending: number, failed = 0, names: string[] = []): MountUploads => ({
  unknown: false,
  pending,
  failed,
  failed_names: names,
});

// -- uploadNotice --------------------------------------------------------------

test("a not-applicable queue (null) says nothing", () => {
  // The server sends null for a read-only or unhealthy mount: no queue is
  // possible there, so there is nothing to report.
  expect(uploadNotice(null).kind).toBe("none");
  expect(uploadNotice(undefined).kind).toBe("none");
});

test("a verified empty queue says nothing", () => {
  expect(uploadNotice(clear).kind).toBe("none");
});

test("an UNREADABLE queue is reported, not silently treated as empty", () => {
  // The regression: `!uploads || uploads.pending === 0` collapsed unknown and
  // verified-empty into one branch, so a mount whose vfs/queue read failed
  // rendered a clean card while files sat unuploaded.
  const notice = uploadNotice({ unknown: true, reason: "rclone daemon is not running" });
  expect(notice.kind).toBe("unknown");
  expect(notice).toMatchObject({ reason: "rclone daemon is not running" });
});

test("failed items outrank pending ones in the message", () => {
  const notice = uploadNotice(queued(5, 2, ["a.tif", "b.tif"]));
  expect(notice).toEqual({
    kind: "failed",
    failed: 2,
    names: ["a.tif", "b.tif"],
    truncated: false,
  });
});

test("a failure count above the names the server sent is marked truncated", () => {
  const notice = uploadNotice(queued(9, 9, ["a.tif", "b.tif", "c.tif"]));
  expect(notice).toMatchObject({ kind: "failed", failed: 9, truncated: true });
});

test("pending-only reports the transfer", () => {
  expect(uploadNotice(queued(3))).toEqual({ kind: "pending", pending: 3 });
});

// -- hasDrainingUploads (the poll gate) ----------------------------------------

test("no mounts, nothing queued, and a clear queue do not schedule polling", () => {
  expect(hasDrainingUploads([])).toBe(false);
  expect(hasDrainingUploads([{ uploads: null }])).toBe(false);
  expect(hasDrainingUploads([{ uploads: clear }])).toBe(false);
});

test("an in-progress upload schedules polling", () => {
  expect(hasDrainingUploads([{ uploads: queued(2) }])).toBe(true);
});

test("a permanently failing upload does NOT poll forever", () => {
  // rclone re-queues a failed item with a doubled delay and never drops it, so
  // `pending` stays above zero for the life of the mount. Gating on pending > 0
  // left an 8s full-probe timer running for the life of the page.
  expect(hasDrainingUploads([{ uploads: queued(2, 2, ["a.tif", "b.tif"]) }])).toBe(false);
});

test("a new failure still wakes the page before it settles", () => {
  // An item counts as pending before its first attempt returns, so the mixed
  // state polls — which is how the failure gets on screen at all.
  expect(hasDrainingUploads([{ uploads: queued(3, 1, ["a.tif"]) }])).toBe(true);
});

test("an unreadable queue does not schedule polling", () => {
  // It is already shown as a warning, and a daemon that cannot answer is the
  // last thing to ask on a timer.
  expect(hasDrainingUploads([{ uploads: { unknown: true, reason: "x" } }])).toBe(false);
});

test("one draining mount among many is enough", () => {
  expect(
    hasDrainingUploads([
      { uploads: null },
      { uploads: queued(1, 1, ["a.tif"]) },
      { uploads: queued(4) },
    ])
  ).toBe(true);
});
