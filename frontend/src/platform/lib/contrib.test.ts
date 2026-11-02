// contrib.ts: what the page sends and when it reads.
//
// Accept and Try carry the head of the review the user read, every time —
// the server refuses a yes that names a head the sender has since moved
// past ("review-needed"), so a flagged proposal is only ever accepted or run
// as the code whose findings were on screen. And the copy-status read runs
// for a known Marketplace copy whatever the switches say: its block asks
// before it opens whatever they say.
//
// fetch is stubbed directly (no mock.module, which is process-wide in bun).
import { afterEach, expect, test } from "bun:test";
import { installDomShim } from "./testDomShim";

installDomShim();
const { acceptProposal, contribReadEnabled, oldIdNote, opensDoctor, tryProposal } = await import("./contrib");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function record(answer: unknown) {
  const sent: { url: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    sent.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
    return new Response(JSON.stringify(answer), { status: 200 });
  }) as unknown as typeof fetch;
  return sent;
}

test("Accept sends the reviewed head with the confirm", async () => {
  const sent = record({ ok: true, job: "j1" });
  await acceptProposal("/w/app", 7, true, "abc123");
  expect(sent).toEqual([
    { url: "/api/contrib/inbox/accept", body: { path: "/w/app", number: 7, confirm: true, head_sha: "abc123" } },
  ]);
});

test("Try sends the reviewed head, confirmed or not", async () => {
  const sent = record({ ok: true });
  await tryProposal("/w/app", 7, false, "abc123");
  await tryProposal("/w/app", 7, true, "abc123");
  expect(sent.map((s) => s.body)).toEqual([
    { path: "/w/app", number: 7, confirm: false, head_sha: "abc123" },
    { path: "/w/app", number: 7, confirm: true, head_sha: "abc123" },
  ]);
});

test("a review-needed refusal comes back as an ordinary answer with the server's sentence", async () => {
  record({ ok: false, reason: "review-needed", message: "alice changed this proposal since its review." });
  const r = await acceptProposal("/w/app", 7, true, "old");
  expect(r).toEqual({ ok: false, reason: "review-needed", message: "alice changed this proposal since its review." });
});

const OFF = { sources: false, changes: false, inbox: false, loop: false, remix: false };

test("the copy-status read runs for a known Marketplace copy before the flags land", () => {
  expect(contribReadEnabled("/w/app", null, false)).toBe(false);
  expect(contribReadEnabled("/w/app", null, true)).toBe(true);
  expect(contribReadEnabled(null, null, true)).toBe(false);
  // No phase on (not a state the always-on loop produces, but the rule holds).
  expect(contribReadEnabled("/w/app", OFF, false)).toBe(false);
});

test("the phases turn the read on by themselves", () => {
  expect(contribReadEnabled("/w/app", { ...OFF, sources: true }, false)).toBe(true);
});

test("a publish refused for Doctor's checks or a missing app id offers App Doctor; nothing else does", () => {
  expect(opensDoctor("doctor")).toBe(true);
  expect(opensDoctor("app-id")).toBe(true);
  expect(opensDoctor("unpushed")).toBe(false);
  expect(opensDoctor(undefined)).toBe(false);
});

test("the old-id note: only when the listing's slug is not the app's own id", () => {
  expect(oldIdNote({ slug: "sine-starter", app_id: "sine-0a1b2c3d", slug_is_app_id: false })).toBe(
    "Listed under the old id sine-starter. Remove it, then publish again to move it to sine-0a1b2c3d.",
  );
  expect(oldIdNote({ slug: "sine-0a1b2c3d", app_id: "sine-0a1b2c3d", slug_is_app_id: true })).toBeNull();
  // No app id yet: App Doctor's pointer says what to do instead.
  expect(oldIdNote({ slug: "sine-starter", app_id: null, slug_is_app_id: false })).toBeNull();
  // An older server that sends neither field.
  expect(oldIdNote({ slug: "sine-starter" })).toBeNull();
  expect(oldIdNote(null)).toBeNull();
});
