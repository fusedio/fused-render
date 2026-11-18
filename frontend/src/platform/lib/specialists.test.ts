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
  closeSpecialistBreakoutCard,
  deliverComposerDraft,
  getSpecialistBreakouts,
  openSpecialistBreakout,
  resetSpecialistBreakouts,
  updateSpecialistBreakout,
} from "./specialist-breakouts";
import { COMPOSER_DRAFT_EVENT, type ComposerDraftDetail } from "./breakouts";
import {
  addNote,
  clearChat,
  createBreakout,
  deleteNote,
  patchSpecialist,
  putConnections,
  revertMessage,
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

describe("deliverComposerDraft", () => {
  const detail: ComposerDraftDetail = {
    sessionId: "s1",
    file: "/w/proj",
    text: "Outcome: solved",
    label: "Draft from the Canvas specialist · not sent yet",
  };

  test("a mounted task chat takes it (with its label); nothing is saved", async () => {
    let seen: ComposerDraftDetail | null = null;
    const on = (e: Event) => {
      seen = (e as CustomEvent<ComposerDraftDetail>).detail;
      e.preventDefault();
    };
    window.addEventListener(COMPOSER_DRAFT_EVENT, on);
    const saves: string[] = [];
    const ok = await deliverComposerDraft(detail, (_k, t) => (saves.push(t), { ok: true }), () => Promise.resolve(null));
    window.removeEventListener(COMPOSER_DRAFT_EVENT, on);
    expect(ok).toBe(true);
    expect(seen!.label).toBe(detail.label!);
    expect(saves).toEqual([]);
  });

  test("no chat mounted: appended below the saved draft", async () => {
    const saves: string[] = [];
    const ok = await deliverComposerDraft(
      detail,
      (_k, t) => (saves.push(t), { ok: true }),
      () => Promise.resolve({ text: "my words", attachments: [] } as never),
    );
    expect(ok).toBe(true);
    expect(saves).toEqual(["my words\n\nOutcome: solved"]);
  });

  test("no chat mounted and nothing typed: the summary alone", async () => {
    const saves: string[] = [];
    await deliverComposerDraft(detail, (_k, t) => (saves.push(t), { ok: true }), () => Promise.resolve(null));
    expect(saves).toEqual(["Outcome: solved"]);
  });

  test("an unreadable saved draft is never overwritten", async () => {
    const saves: string[] = [];
    const ok = await deliverComposerDraft(detail, (_k, t) => (saves.push(t), { ok: true }), () => Promise.resolve(undefined));
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

  test("the fallback Revert message names the change and its commit", () => {
    const m = revertMessage({ id: "i1", kind: "New skill", text: "Publish checklist", commit: "abc123", undo: "delete it" }, "r1");
    expect(m).toContain('New skill, "Publish checklist"');
    expect(m).toContain("commit abc123");
  });
});
