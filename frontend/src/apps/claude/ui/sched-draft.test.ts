// The scheduler handoff's two round trips: the draft (T:12105-12118) and the
// attachments beside it (owner E2E R1, F4 (2026-09-10)).
//
// `sessionStorage` is NOT in `testDomShim` — nothing else in the suite needs it,
// and the shim's own note says to extend it only for `window`/`location`/
// `history` members. A map-backed stand-in is installed here, before the module
// under test is imported, for the same ordering reason: `??=` so a suite that
// ran first in this process keeps its own.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

interface Storeish {
  sessionStorage?: unknown;
}
const rows = new Map<string, string>();
(globalThis as Storeish).sessionStorage ??= {
  getItem: (k: string) => (rows.has(k) ? rows.get(k)! : null),
  setItem: (k: string, v: string) => void rows.set(k, String(v)),
  removeItem: (k: string) => void rows.delete(k),
  clear: () => rows.clear(),
};

const {
  attachKey,
  basenameOf,
  draftKey,
  parseAttachmentsParam,
  schedulerUrl,
  stashAttachments,
  stashDraft,
  takeAttachments,
  takeDraft,
} = await import("./sched-draft");

type SchedAttachment = import("./sched-draft").SchedAttachment;

const FILE = "/w/app/page.html";
const LINK = { file: FILE, draft: "look at this", sessionId: "s1", back: "/w/app/page.html" };
const SHOTS: SchedAttachment[] = [
  { path: "/Users/a/.fused-render/task-shots/20260910-a.png", name: "shot.png", kind: "image" },
  { path: "/Users/a/.fused-render/task-shots/20260910-b.csv", name: "rows.csv", kind: "file" },
];

test("the two stashes are DIFFERENT rows — each half of the handoff is spent on its own", () => {
  expect(attachKey(FILE)).not.toBe(draftKey(FILE));
  expect(attachKey(null)).toBe("fused:chatshots:");
});

test("the attachments round-trip, and the row is SPENT — a second mount gets nothing", () => {
  stashAttachments(FILE, SHOTS);
  expect(takeAttachments(FILE)).toEqual(SHOTS);
  // Spent either way, exactly like the draft: re-filling the tray over what the
  // user has attached since is the bug this rule exists for.
  expect(takeAttachments(FILE)).toEqual([]);
});

test("the two halves do not read each other's row", () => {
  stashDraft(FILE, "words");
  stashAttachments(FILE, SHOTS);
  expect(takeDraft(FILE)).toBe("words");
  expect(takeAttachments(FILE)).toEqual(SHOTS);
});

test("an EMPTY list clears the row rather than storing []", () => {
  stashAttachments(FILE, SHOTS);
  stashAttachments(FILE, []);
  expect(takeAttachments(FILE)).toEqual([]);
});

test("the stash is per FILE — a handoff from another folder is another errand", () => {
  stashAttachments(FILE, SHOTS);
  expect(takeAttachments("/w/other/page.html")).toEqual([]);
  expect(takeAttachments(FILE)).toEqual(SHOTS);
});

test("schedulerUrl appends the attachments, decodably, and ONLY when there are some", () => {
  expect(schedulerUrl(LINK)).not.toContain("attachments=");
  const url = schedulerUrl({ ...LINK, attachments: SHOTS });
  const raw = new URLSearchParams(url.slice(url.indexOf("?"))).get("attachments");
  expect(JSON.parse(raw!)).toEqual(SHOTS);
  // The rest of the link is untouched — the param is an addition, not a shape
  // change (T:12019's `new=1` still opens the form).
  expect(url.startsWith("/tasks?new=1")).toBe(true);
  expect(url).toContain("&message=look%20at%20this");
});

test("an empty attachments list is the same URL as no attachments at all", () => {
  expect(schedulerUrl({ ...LINK, attachments: [] })).toBe(schedulerUrl(LINK));
});

test("BAD JSON IS AN EMPTY LIST, never a throw — the effect that opens the form runs on it", () => {
  expect(parseAttachmentsParam(null)).toEqual([]);
  expect(parseAttachmentsParam("")).toEqual([]);
  expect(parseAttachmentsParam("[{oops")).toEqual([]);
  expect(parseAttachmentsParam('{"path":"/a.png"}')).toEqual([]);
  expect(parseAttachmentsParam("[1,null,\"x\"]")).toEqual([]);
});

test("a row with no path is dropped, and everything else is floored", () => {
  expect(
    parseAttachmentsParam(
      JSON.stringify([
        { path: "", name: "gone", kind: "image" },
        { name: "pathless", kind: "image" },
        { path: "/shots/x.png", kind: "image" },
        { path: "/shots/y.tif", name: "y.tif", kind: "wat" },
      ]),
    ),
  ).toEqual([
    // No name on the wire: the basename is the only name there is.
    { path: "/shots/x.png", name: "x.png", kind: "image" },
    // Anything that is not the word "image" wears the glyph — the same floor
    // `restoredAttachments` applies to a stored entry.
    { path: "/shots/y.tif", name: "y.tif", kind: "file" },
  ]);
});

test("a stashed row from an older build is read by the same parser", () => {
  // Written by hand rather than by `stashAttachments`, which is the case this
  // shared parser exists for.
  (globalThis as { sessionStorage: Storage }).sessionStorage.setItem(
    attachKey(FILE),
    '[{"path":"/shots/z.png"},"junk"]',
  );
  expect(takeAttachments(FILE)).toEqual([
    { path: "/shots/z.png", name: "z.png", kind: "file" },
  ]);
});

test("basenameOf survives both separators and a path that is only a name", () => {
  expect(basenameOf("/shots/a/b.png")).toBe("b.png");
  expect(basenameOf("C:\\shots\\b.png")).toBe("b.png");
  expect(basenameOf("b.png")).toBe("b.png");
  // Never the empty string: a chip with no name at all says nothing.
  expect(basenameOf("/shots/")).toBe("/shots/");
});
