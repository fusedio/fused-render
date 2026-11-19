// The platform half of the specialists feature: the flag store, the breakout
// card store, the composer-draft hand-off and the typed routes.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";

import {
  feedSpecialistsEnabled,
  publishSpecialistsEnabled,
  resetSpecialistsFlagForTests,
  specialistsEnabledNow,
  specialistsFlagStamp,
} from "./specialists-flag";
import {
  addSummaryDraft,
  closeSpecialistBreakoutCard,
  deliverComposerDraft,
  getSpecialistBreakouts,
  loadSummaryDraft,
  readSummaryDraft,
  resolveSummaryDraft,
  summaryDraftFromRecords,
  resetSummaryDraftsForTests,
  summaryDraftText,
  openSpecialistBreakout,
  resetSpecialistBreakouts,
  updateSpecialistBreakout,
} from "./specialist-breakouts";
import { COMPOSER_DRAFT_EVENT, type ComposerDraftDetail } from "./breakouts";
import {
  addNote,
  clearChat,
  confirmNote,
  createBreakout,
  moveGeneralNote,
  setBreakoutDraft,
  type BreakoutRecord,
  deleteNote,
  patchSpecialist,
  putConnections,
  revertMessage,
  REVERT_PREFIX,
  serviceKeyForTool,
} from "./specialists-api";

// A private event bus for window events, restored after the suite.
const bus = new EventTarget();
const w = window as unknown as Record<string, unknown>;
const saved = { add: w.addEventListener, remove: w.removeEventListener, dispatch: w.dispatchEvent };
beforeAll(() => {
  w.addEventListener = bus.addEventListener.bind(bus);
  w.removeEventListener = bus.removeEventListener.bind(bus);
  w.dispatchEvent = bus.dispatchEvent.bind(bus);
});
afterAll(() => {
  w.addEventListener = saved.add;
  w.removeEventListener = saved.remove;
  w.dispatchEvent = saved.dispatch;
});

describe("specialists flag", () => {
  afterEach(() => resetSpecialistsFlagForTests());

  test("off until told; a publish turns it on", () => {
    resetSpecialistsFlagForTests();
    expect(specialistsEnabledNow()).toBe(false);
    publishSpecialistsEnabled(true);
    expect(specialistsEnabledNow()).toBe(true);
    publishSpecialistsEnabled(false);
    expect(specialistsEnabledNow()).toBe(false);
  });

  test("another reader's answer is dropped when a publish happened while it was in flight", () => {
    resetSpecialistsFlagForTests();
    const stamp = specialistsFlagStamp();
    publishSpecialistsEnabled(true); // the toggle
    feedSpecialistsEnabled(false, stamp); // a prefs read that departed before it
    expect(specialistsEnabledNow()).toBe(true);
    feedSpecialistsEnabled(false, specialistsFlagStamp());
    expect(specialistsEnabledNow()).toBe(false);
  });
});

describe("specialist breakout cards", () => {
  afterEach(() => resetSpecialistBreakouts());
  const base = {
    breakoutId: "b-1",
    specialistId: "c1",
    specialistName: "Canvas",
    home: "/w/specialists/canvas",
    parentSessionId: "s1",
    parentFile: "/w/proj",
    startParams: { specialist: "c1:breakout:b-1" },
  };

  test("one card per breakout: a second open raises it and takes the newer facts", () => {
    const a = openSpecialistBreakout({ ...base, seed: "Brief" });
    openSpecialistBreakout({ ...base, breakoutId: "b-2" });
    const again = openSpecialistBreakout({ ...base, sessionId: "bs1" });
    expect(again).toBe(a);
    const cards = getSpecialistBreakouts();
    expect(cards).toHaveLength(2);
    expect(cards[1]!.id).toBe(a);
    expect(cards[1]!.sessionId).toBe("bs1");
  });

  test("update drops a key set to undefined (the spent seed); close removes it", () => {
    const id = openSpecialistBreakout({ ...base, seed: "Brief" });
    updateSpecialistBreakout(id, { seed: undefined });
    expect("seed" in getSpecialistBreakouts()[0]!).toBe(false);
    closeSpecialistBreakoutCard(id);
    expect(getSpecialistBreakouts()).toHaveLength(0);
  });
});

function close(key: string, draft: string, draft_state: "pending" | "sent" | "cleared" | undefined, at: number) {
  return { at, note_id: "", draft, key, ...(draft_state ? { draft_state } : {}) };
}
function rec(id: string, name: string, status: string, closes: BreakoutRecord["closes"]): BreakoutRecord {
  return {
    id,
    specialist: `c-${name}`,
    name,
    origin: "hand",
    parent: { session_id: "s1", file: "/w/proj" },
    brief: { problem: "p", tried: "", look_at: "" },
    session_id: "bs",
    status,
    opened: 0,
    closes,
    discarded: null,
  };
}

describe("deliverComposerDraft", () => {
  const detail: ComposerDraftDetail = {
    sessionId: "s1",
    file: "/w/proj",
    text: "Outcome: solved",
    label: "Draft from the Canvas specialist · not sent yet",
  };
  const plain: ComposerDraftDetail = { sessionId: "s1", file: "/w/proj", text: "Outcome: solved" };
  // The server: GET …/breakouts answers `records`; every request is noted.
  const realFetch = globalThis.fetch;
  let records: BreakoutRecord[] = [];
  let calls: { url: string; method: string; body: unknown }[] = [];
  beforeAll(() => {
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method || "GET", body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return new Response(JSON.stringify({ breakouts: records }), { status: 200 });
    }) as typeof fetch;
  });
  afterAll(() => {
    globalThis.fetch = realFetch;
  });
  afterEach(() => {
    resetSummaryDraftsForTests();
    records = [];
    calls = [];
  });

  test("a specialist's summary becomes the task's summary draft; the composer and its saved draft are untouched", async () => {
    let events = 0;
    const on = () => void (events += 1);
    window.addEventListener(COMPOSER_DRAFT_EVENT, on);
    const saves: string[] = [];
    const ok = await deliverComposerDraft(
      { ...detail, name: "Canvas", breakoutId: "b-1", specialistId: "c1", close: "k1" },
      (_k, t) => (saves.push(t), { ok: true }),
      () => Promise.resolve(null),
    );
    window.removeEventListener(COMPOSER_DRAFT_EVENT, on);
    expect(ok).toBe(true);
    expect(events).toBe(0);
    expect(saves).toEqual([]);
    // shown at once, then read back from the server (which holds it pending)
    const parts = readSummaryDraft("s1");
    expect(parts.map((p) => [p.text, p.label, p.name, p.close])).toEqual([["Outcome: solved", detail.label!, "Canvas", "k1"]]);
    expect(calls.map((c) => [c.method, c.url])).toEqual([["GET", "/api/specialists/breakouts?parent_session_id=s1"]]);
  });

  test("no browser storage: the draft is whatever the server says is pending", async () => {
    const g = globalThis as unknown as { localStorage?: unknown };
    const had = g.localStorage;
    let touched = 0;
    g.localStorage = new Proxy({}, { get: () => (touched += 1, () => null) });
    records = [
      rec("b-1", "Canvas", "closed", [close("k1", "Outcome: solved", "sent", 1), close("k2", "Outcome: workaround", "pending", 3)]),
      rec("b-2", "Data", "discarded", [close("k3", "Outcome: dropped", "pending", 2)]),
      rec("b-3", "Data", "open", [close("k4", "Outcome: old page", undefined, 2)]),
    ];
    expect(await loadSummaryDraft("s1")).toBe(true);
    expect(readSummaryDraft("s1").map((p) => [p.text, p.name, p.specialistId, p.breakoutId, p.close, p.label])).toEqual([
      ["Outcome: workaround", "Canvas", "c-Canvas", "b-1", "k2", "Draft from the Canvas specialist · not sent yet"],
    ]);
    records = [];
    await loadSummaryDraft("s1");
    expect(readSummaryDraft("s1")).toHaveLength(0);
    g.localStorage = had;
    expect(touched).toBe(0);
  });

  test("a second summary is added below; Send/Clear mark each close on the server and drop it", async () => {
    records = [rec("b-1", "Canvas", "closed", [close("k1", "Outcome: solved", "pending", 1), close("k2", "Outcome: workaround", "pending", 2)])];
    await loadSummaryDraft("s1");
    const parts = readSummaryDraft("s1");
    expect(summaryDraftText(parts)).toBe("Outcome: solved\n\nOutcome: workaround");
    expect(await resolveSummaryDraft("s1", parts, "sent")).toBe(true);
    expect(calls.filter((c) => c.method === "POST").map((c) => [c.url, c.body])).toEqual([
      ["/api/specialists/c-Canvas/breakouts/b-1/draft", { close: "k1", state: "sent" }],
      ["/api/specialists/c-Canvas/breakouts/b-1/draft", { close: "k2", state: "sent" }],
    ]);
    expect(readSummaryDraft("s1")).toHaveLength(0);
  });

  test("a failed mark keeps that part; a gone close (404/409/410) counts as done", async () => {
    addSummaryDraft("s1", { text: "A", label: "L", name: "Canvas", specialistId: "c1", breakoutId: "b-1", close: "k1", at: 1 });
    addSummaryDraft("s1", { text: "B", label: "L", name: "Canvas", specialistId: "c1", breakoutId: "b-1", close: "k2", at: 2 });
    const mark = (_id: string, _bid: string, k: string) =>
      Promise.reject(Object.assign(new Error("x"), { status: k === "k1" ? 409 : 500 }));
    expect(await resolveSummaryDraft("s1", readSummaryDraft("s1"), "cleared", mark)).toBe(false);
    expect(readSummaryDraft("s1").map((p) => p.close)).toEqual(["k2"]);
  });

  test("a read that started before a local change does not bring a resolved draft back", async () => {
    addSummaryDraft("s1", { text: "A", label: "L", name: "Canvas", specialistId: "c1", breakoutId: "b-1", close: "k1", at: 1 });
    let answer!: (v: { breakouts: BreakoutRecord[] }) => void;
    const slow = loadSummaryDraft("s1", () => new Promise((res) => (answer = res)));
    await resolveSummaryDraft("s1", readSummaryDraft("s1"), "cleared", () => Promise.resolve({}));
    answer({ breakouts: [rec("b-1", "Canvas", "closed", [close("k1", "A", "pending", 1)])] });
    await slow;
    expect(readSummaryDraft("s1")).toHaveLength(0);
  });

  test("setBreakoutDraft posts {close, state} to …/draft", async () => {
    await setBreakoutDraft("c1", "b-1", 0, "cleared");
    expect(calls[0]).toEqual({ url: "/api/specialists/c1/breakouts/b-1/draft", method: "POST", body: { close: 0, state: "cleared" } });
  });

  test("summaryDraftFromRecords tolerates junk", () => {
    expect(summaryDraftFromRecords(null)).toEqual([]);
    expect(summaryDraftFromRecords([rec("b-1", "Canvas", "closed", [null as never])])).toEqual([]);
  });

  test("plain (no label): a mounted task chat takes it; nothing is saved", async () => {
    let seen: ComposerDraftDetail | null = null;
    const on = (e: Event) => {
      seen = (e as CustomEvent<ComposerDraftDetail>).detail;
      e.preventDefault();
    };
    window.addEventListener(COMPOSER_DRAFT_EVENT, on);
    const saves: string[] = [];
    const ok = await deliverComposerDraft(plain, (_k, t) => (saves.push(t), { ok: true }), () => Promise.resolve(null));
    window.removeEventListener(COMPOSER_DRAFT_EVENT, on);
    expect(ok).toBe(true);
    expect(seen!.text).toBe("Outcome: solved");
    expect(saves).toEqual([]);
  });

  test("plain, no chat mounted: appended below the saved draft", async () => {
    const saves: string[] = [];
    const ok = await deliverComposerDraft(
      plain,
      (_k, t) => (saves.push(t), { ok: true }),
      () => Promise.resolve({ text: "my words", attachments: [] } as never),
    );
    expect(ok).toBe(true);
    expect(saves).toEqual(["my words\n\nOutcome: solved"]);
  });

  test("plain: an unreadable saved draft is never overwritten", async () => {
    const saves: string[] = [];
    const ok = await deliverComposerDraft(plain, (_k, t) => (saves.push(t), { ok: true }), () => Promise.resolve(undefined));
    expect(ok).toBe(false);
    expect(saves).toEqual([]);
  });
});

describe("specialists api", () => {
  const realFetch = globalThis.fetch;
  let seen: { url: string; method: string; body: unknown; headers: Record<string, string> }[] = [];
  beforeAll(() => {
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      seen.push({
        url,
        method: init?.method || "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        headers: (init?.headers || {}) as Record<string, string>,
      });
      return new Response(JSON.stringify({ ok: true, current: null, folded: [] }), { status: 200 });
    }) as typeof fetch;
  });
  afterAll(() => {
    globalThis.fetch = realFetch;
  });
  afterEach(() => {
    seen = [];
  });

  test("Clear posts to …/chat/clear with the write guard", async () => {
    await clearChat("c1");
    expect(seen[0]).toMatchObject({ url: "/api/specialists/c1/chat/clear", method: "POST" });
    expect(seen[0]!.headers["X-Fused"]).toBe("1");
  });

  test("every write carries X-Fused: 1 (POST, PUT, PATCH, DELETE)", async () => {
    await clearChat("c1");
    await putConnections("c1", {});
    await patchSpecialist("c1", { name: "x" });
    await deleteNote("c1", "n-1");
    await confirmNote("c1", "n-1");
    await moveGeneralNote("n-2", "c1");
    expect(seen.map((s) => s.headers["X-Fused"])).toEqual(["1", "1", "1", "1", "1", "1"]);
    expect(seen.slice(-2).map((s) => [s.method, s.url, s.body])).toEqual([
      ["POST", "/api/specialists/c1/notes/n-1/confirm", {}],
      ["POST", "/api/specialists/general/notes/n-2/move", { specialist: "c1" }],
    ]);
  });

  test("a note goes to the specialist or the general inbox; Undo deletes it", async () => {
    await addNote("general", { kind: "case", claim: "c", basis: "user-said", source: { session_id: "s1" } });
    await deleteNote("c1", "n-1");
    expect(seen.map((s) => [s.method, s.url])).toEqual([
      ["POST", "/api/specialists/general/notes"],
      ["DELETE", "/api/specialists/c1/notes/n-1"],
    ]);
    expect(seen[0]!.body).toMatchObject({ basis: "user-said", source: { session_id: "s1" } });
  });

  test("a breakout by hand posts parent, brief and origin", async () => {
    await createBreakout("c1", {
      parent: { session_id: "s1", file: "/w/proj" },
      brief: { problem: "p", tried: "t", look_at: "l" },
      origin: "hand",
    });
    expect(seen[0]).toMatchObject({ url: "/api/specialists/c1/breakouts", method: "POST" });
    expect(seen[0]!.body).toMatchObject({ origin: "hand", brief: { problem: "p" } });
  });

  test("PATCH and connections", async () => {
    await patchSpecialist("c1", { name: "Canvas" });
    await putConnections("c1", { add: ["slack"] });
    expect(seen.map((s) => s.method)).toEqual(["PATCH", "PUT"]);
    expect(seen[1]!.body).toEqual({ add: ["slack"], remove: [] });
  });

  test("a services tool maps back to its allowed-list key (longest match)", () => {
    const services = [
      { key: "slack", title: "Slack", kind: "connect", connected: true, allowed: false },
      { key: "slack_admin", title: "Slack admin", kind: "connect", connected: true, allowed: false },
      { key: "app:/w/apps/notes", title: "Notes", kind: "app", connected: true, allowed: false },
    ];
    expect(serviceKeyForTool("mcp__specialist_services__slack_post_message", services)).toBe("slack");
    expect(serviceKeyForTool("mcp__specialist_services__slack_admin_ban", services)).toBe("slack_admin");
    expect(serviceKeyForTool("mcp__specialist_services__app__w_apps_notes_add", services)).toBe("app:/w/apps/notes");
    expect(serviceKeyForTool("Bash", services)).toBeNull();
  });

  test("the fallback Revert message starts with the C3 prefix, quotes the change, and names no commit (R1, C4)", () => {
    const m = revertMessage({ id: "i1", kind: "New skill", text: "Publish checklist", commit: "abc123", undo: "delete it" }, "r1");
    expect(m.startsWith(REVERT_PREFIX + '"Publish checklist"')).toBe(true);
    expect(m).not.toContain("abc123");
    expect(m).toContain("delete it");
  });
});
