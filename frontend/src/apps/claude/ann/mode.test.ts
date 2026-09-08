// EVERY ROW of inventory 02 §D's transition table, in the table's order.
// The recorder is a fake (the real one is `ann/rec*`): it reports `recording()`
// and `settling()` and the test drives the phases the way the real one does.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { createMemoryParamsStore } = await import("../params/store");
const { createAnnStore } = await import("./store");
const { createAnnMode, escapeAction } = await import("./mode");
import type { AnnMode, AnnRecorder } from "./types";

interface Rig {
  machine: ReturnType<typeof createAnnMode>;
  store: ReturnType<typeof createAnnStore>;
  params: ReturnType<typeof createMemoryParamsStore>;
  log: string[];
  rec: {
    on: boolean;
    phase: null | "settling" | "transcribing";
    ended: number;
    discarded: number;
  };
  /** Whatever the recorder module would do on a stop: flags down BEFORE the
   *  await, the settle's status up, the nav lock claimed (T:8111-8170). */
  beginSettle(): void;
  finishSettle(to: "off" | "transcribing"): void;
  capable: { value: boolean };
  canSend: { value: boolean };
  composer: { open: boolean; text: string };
  /** Resolves the pending `commitDraft`, so a test can hold the await open the
   *  way a real save does. */
  releaseCommit(): void;
}

function rig(opts: { params?: Record<string, string>; capable?: boolean } = {}): Rig {
  const params = createMemoryParamsStore(opts.params ?? {});
  let t = 1000;
  const store = createAnnStore({ params, now: () => t++, newId: () => "id" + t });
  const log: string[] = [];
  const rec = { on: false, phase: null as null | "settling" | "transcribing", ended: 0, discarded: 0 };
  const capable = { value: opts.capable ?? true };
  const canSend = { value: true };
  const composer = { open: false, text: "" };
  let releaseCommit: () => void = () => {};
  const recorder: AnnRecorder = {
    recording: () => rec.on,
    settling: () => rec.phase !== null,
    end: () => {
      rec.ended++;
      log.push("rec.end");
    },
    discard: () => {
      rec.discarded++;
      log.push("rec.discard");
    },
  };
  const machine = createAnnMode({
    store,
    capable: () => capable.value,
    recorder: () => recorder,
    render: () => log.push("render"),
    onLock: (l) => log.push("lock:" + l),
    onToolVisible: (s) => log.push("tool:" + s),
    composerOpen: () => composer.open,
    commitDraft: () => {
      log.push("commit:" + composer.text);
      if (composer.text) store.add({ content: composer.text });
      composer.open = false;
      return new Promise<void>((res) => {
        releaseCommit = res;
      });
    },
    closeComposer: () => {
      composer.open = false;
      log.push("close");
    },
    hideHl: () => log.push("hideHl"),
    autoSubmit: () => log.push("submit"),
    canSend: () => canSend.value,
    xo: () => false,
    now: () => 5000,
  });
  const self: Rig = {
    machine,
    store,
    params,
    log,
    rec,
    capable,
    canSend,
    composer,
    releaseCommit: () => releaseCommit(),
    beginSettle() {
      // The real recorder's order: flags DOWN before the await, then the status
      // and the lock claim (Bugbot, PR #665).
      rec.on = false;
      rec.phase = "settling";
      machine.setBusyHold(true);
      machine.setPhase("settling");
    },
    finishSettle(to) {
      if (to === "transcribing") {
        rec.phase = "transcribing";
        machine.setPhase("transcribing");
        return;
      }
      rec.phase = null;
      machine.setPhase(null);
      machine.set(false);
    },
  };
  return self;
}

const modeOf = (r: Rig): AnnMode => r.machine.mode();

describe("§D — off → comment", () => {
  test("the Comment seat, and `annSetMode(true)`: epoch bumps, round restarts, param says 1", () => {
    const r = rig();
    expect(modeOf(r)).toBe("off");
    r.machine.set(true);
    expect(modeOf(r)).toBe("comment");
    expect(r.machine.epoch()).toBe(1);
    expect(r.store.roundStart()).toBe(5000);
    expect(r.params.get("annmode")).toBe("1");
    expect(r.log).toContain("tool:true");
    expect(r.log).toContain("lock:true");
  });

  test('the boot default arms only on exactly "1" (T:7745)', () => {
    const on = rig({ params: { annmode: "1" } });
    on.machine.bootFromParam();
    expect(modeOf(on)).toBe("comment");

    const off = rig({ params: { annmode: "0" } });
    off.machine.bootFromParam();
    expect(modeOf(off)).toBe("off");
  });

  test('§D last row — `annmode=2` on boot ENDS the walkthrough, it does not resume it', () => {
    const r = rig({ params: { annmode: "2" } });
    r.machine.bootFromParam();
    expect(modeOf(r)).toBe("off");
    expect(r.rec.on).toBe(false);
  });

  test("nothing to point at can never be armed, and the param is NOT rewritten", () => {
    const r = rig({ params: { annmode: "1" }, capable: false });
    r.machine.bootFromParam();
    expect(modeOf(r)).toBe("off");
    expect(r.params.get("annmode")).toBe("1"); // ignored, not rewritten
  });
});

describe("§D — the recording rows", () => {
  test("off + mic → recording, and the mode is armed under it", () => {
    const r = rig();
    r.machine.set(true); // the recorder arms the mode if it was off
    r.rec.on = true;
    expect(modeOf(r)).toBe("recording");
  });

  test("comment + mic click is INERT — the strip's guard, not the machine's", () => {
    const r = rig();
    r.machine.set(true);
    expect(modeOf(r)).toBe("comment");
    // Nothing here changes the mode; the seat's own handler returns null.
    expect(r.rec.ended).toBe(0);
  });

  test("recording + a click in the app stays recording (the mark is the store's)", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.store.add({ content: "", t: 1.2 });
    expect(modeOf(r)).toBe("recording");
  });

  test("recording + `set(false)` ends the mic rather than leaving it running", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.machine.set(false);
    expect(r.rec.ended).toBe(1);
  });

  test("recording + the target disappearing releases the mic (T:7615)", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.capable.value = false;
    r.machine.targetGone();
    expect(r.rec.ended).toBe(1);
    expect(r.machine.armed()).toBe(false);
  });

  test("the settle: recording → settling → transcribing → off, with the lock held", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.beginSettle();
    expect(modeOf(r)).toBe("settling");
    expect(r.machine.locked()).toBe(true); // busyHold + a phase
    r.finishSettle("transcribing");
    expect(modeOf(r)).toBe("transcribing");
    r.finishSettle("off");
    expect(modeOf(r)).toBe("off");
    expect(r.machine.busyHold()).toBe(false);
  });

  test("a stop that returns nothing lands in off just the same", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.beginSettle();
    r.finishSettle("off");
    expect(modeOf(r)).toBe("off");
  });

  test("bar trash while recording is the recorder's discard, not the notes'", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.store.add({ content: "", t: 1 });
    r.machine.discard();
    expect(r.rec.discarded).toBe(1);
    expect(r.store.list()).toHaveLength(1); // the recorder deletes its own marks
  });
});

describe("§D — comment → off", () => {
  test("Done commits the open draft FIRST, then sends every pending note once", async () => {
    const r = rig();
    r.machine.set(true);
    r.composer.open = true;
    r.composer.text = "make it blue";
    const done = r.machine.done();
    r.releaseCommit();
    await done;
    expect(r.log).toContain("commit:make it blue");
    expect(r.log.filter((l) => l === "submit")).toHaveLength(1);
    expect(modeOf(r)).toBe("off");
  });

  test("nothing pending sends nothing", async () => {
    const r = rig();
    r.machine.set(true);
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(modeOf(r)).toBe("off");
  });

  test("a note with no words is not a message (T:7714's `a.content &&`)", async () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "" });
    await r.machine.done();
    expect(r.log).not.toContain("submit");
  });

  test("`sending` before a run has an id leaves them pending for the next message", async () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "words" });
    r.canSend.value = false;
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(r.store.pending()).toHaveLength(1);
  });

  test("ONE Done at a time (Bugbot #664): a second click inside the commit's await cannot re-send", async () => {
    const r = rig();
    r.machine.set(true);
    r.composer.open = true;
    r.composer.text = "words";
    // The first Done is parked on the commit's await — exactly the window a
    // second click used to slip through, see the just-saved note as merely
    // pending, and send it again.
    const first = r.machine.done();
    const second = r.machine.done();
    r.releaseCommit();
    await Promise.all([first, second]);
    expect(r.log.filter((l) => l === "submit")).toHaveLength(1);
  });

  test("Esc in Comment mode DISCARDS the round (PR #1028) and leaves the mode", () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "this round", createdAt: 6000 });
    r.machine.escape();
    expect(r.store.list()).toHaveLength(0);
    expect(modeOf(r)).toBe("off");
    expect(r.log).toContain("close");
  });

  test("the bar's trash is the same exit", () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "this round", createdAt: 6000 });
    r.machine.discard();
    expect(r.store.list()).toHaveLength(0);
    expect(modeOf(r)).toBe("off");
  });

  test("arriving at the narrow CHAT view disarms (T:8940)", () => {
    const r = rig();
    r.machine.set(true);
    r.machine.arriveNarrowChat();
    expect(modeOf(r)).toBe("off");
  });

  test("…and at boot, with nothing armed, it is a no-op", () => {
    const r = rig();
    const before = r.log.length;
    r.machine.arriveNarrowChat();
    expect(r.log).toHaveLength(before);
  });

  test("the target disappearing disarms without rewriting the param (T:8479)", () => {
    const r = rig({ params: { annmode: "1" } });
    r.machine.set(true);
    r.capable.value = false;
    r.machine.targetGone();
    expect(modeOf(r)).toBe("off");
    expect(r.params.get("annmode")).toBe("1");
  });
});

describe("§D — the settle refuses to be thrown away", () => {
  test("the notes discard REFUSES during a settle (Bugbot #1008)", () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "a mark", createdAt: 6000 });
    r.rec.on = true;
    r.beginSettle();
    r.machine.discard();
    expect(r.store.list()).toHaveLength(1);
    expect(modeOf(r)).toBe("settling");
  });

  test("Esc through the settle only LEAVES the mode — and releases the nav lock", () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "a mark", createdAt: 6000 });
    r.rec.on = true;
    r.beginSettle();
    expect(r.machine.locked()).toBe(true);
    r.machine.escape();
    expect(r.store.list()).toHaveLength(1); // the marks are the recording's
    expect(r.machine.armed()).toBe(false);
    expect(r.machine.busyHold()).toBe(false);
    expect(r.machine.locked()).toBe(false);
  });

  test("a re-arm inside the settle gets a NEW epoch, and the settle still owns the bar", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.beginSettle();
    const armed = r.machine.epoch();
    r.machine.escape();
    // A new arm during the settle owns the mode now, and the disarm decided by
    // the transcription that is still running must leave it alone (Bugbot #644).
    r.machine.set(true);
    expect(r.machine.epoch()).toBe(armed + 1);
    expect(r.machine.armed()).toBe(true);
    // ARMED AND SETTLING AT ONCE is a real state in T (`annOn` plus
    // `#anncta.busy`), and the settle is the half that wins the WORD: the bar is
    // hidden while `.busy` (T:6822) and the notes discard refuses (T:8404),
    // while the pins — which read `armed()`, not the word — are back.
    expect(modeOf(r)).toBe("settling");
    r.finishSettle("off");
  });
});

describe("the nav lock (T:6864)", () => {
  test("locked while armed, and through a settle the mode still holds", () => {
    const r = rig();
    expect(r.machine.locked()).toBe(false);
    r.machine.set(true);
    expect(r.machine.locked()).toBe(true);
    r.rec.on = true;
    r.beginSettle();
    expect(r.machine.locked()).toBe(true);
    r.finishSettle("off");
    expect(r.machine.locked()).toBe(false);
  });

  test("a phase with no claim on it does not lock on its own", () => {
    const r = rig();
    r.machine.setPhase("transcribing");
    expect(r.machine.locked()).toBe(false);
  });
});

describe("who claims Escape (T:15950)", () => {
  test("the viewer, then the composer, then the mode — and nothing else", () => {
    expect(escapeAction(true, true, true)).toBe("close-viewer");
    expect(escapeAction(false, true, true)).toBe("close-composer");
    expect(escapeAction(false, false, true)).toBe("exit-annotate");
    expect(escapeAction(false, false, false)).toBe("");
  });
});
