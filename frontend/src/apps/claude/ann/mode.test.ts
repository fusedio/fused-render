// EVERY ROW of inventory 02 §D's transition table, in the table's order.
// The recorder is a fake (the real one is `ann/rec*`): it reports `recording()`
// and `settling()` and the test drives the phases the way the real one does.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { createMemoryParamsStore } = await import("../params/store");
const { createAnnStore } = await import("./store");
const { createAnnMode, escapeAction, walkthroughOwns } = await import("./mode");
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
    /** The TEARDOWN's ending (`forceOff`), counted apart from the other two. */
    abandoned: number;
  };
  /** The stub itself, so a test can assert what the MACHINE is being told —
   *  `settling()` reads the recorder's phase, and a test that moves only the
   *  React echo (`setPhase`) leaves it answering false (R1-final-b). */
  recorder: AnnRecorder;
  /** Whatever the recorder module would do on a stop: flags down BEFORE the
   *  await, the settle's status up, the nav lock claimed (T:8111-8170). */
  beginSettle(): void;
  /** THE LAG WINDOW: the recorder's own flags move (`recording()` down,
   *  `settling()` up) and the React effect that echoes them into `setPhase` has
   *  NOT run yet. One commit wide in the app; a whole test here. */
  beginSettleUnechoed(): void;
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
  const rec = { on: false, phase: null as null | "settling" | "transcribing", ended: 0, discarded: 0, abandoned: 0 };
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
    // The teardown's ending — never the mode machine's to call (a disarm still
    // means `end()`, which KEEPS and transcribes); logged so a test would see
    // it if that ever changed.
    abandon: () => {
      rec.abandoned++;
      log.push("rec.abandon");
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
    recorder,
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
    beginSettleUnechoed() {
      rec.on = false;
      rec.phase = "settling";
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

  // THE MIC PROMPT'S WINDOW. `AnnRecorder.recording()` counts "starting", so
  // the machine reads the whole window as a recording — before that it called
  // it Comment mode: the bar showed ✓ Done, the Comment seat came alive beside
  // a mic that was about to open, and `set(false)` skipped `end()`, so nothing
  // could cancel the capture still on its way (Bugbot, PR #1074).
  test("the START window is recording: the recording face, and a dismissal reaches end()", () => {
    const r = rig();
    // Straight for the mic, with nothing armed underneath YET: the recorder
    // arms the mode itself at the press (`rec.ts`, Bugbot PR #1074), and this
    // rig stands in for the window between the two — the state the machine has
    // to read as a recording whichever order the two land in.
    r.rec.on = true;
    expect(modeOf(r)).toBe("recording");
    r.machine.set(false);
    expect(r.rec.ended).toBe(1);
    // …and Esc is the same exit while the request is still out.
    const esc = rig();
    esc.rec.on = true;
    esc.machine.escape();
    expect(esc.rec.ended).toBe(1);
    expect(esc.rec.discarded).toBe(0); // Esc STOPS, it does not throw away
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

  test("a note with neither words nor a stamp is not a message (`isSendable`)", async () => {
    const r = rig();
    r.machine.set(true);
    // The empty card a single click in Comment mode leaves behind: no words, no
    // walkthrough stamp, nothing to say.
    r.store.add({ content: "" });
    await r.machine.done();
    expect(r.log).not.toContain("submit");
  });

  test("a WORDLESS STAMPED note is a message, and Done sends it", async () => {
    // The one rule, read three ways: `overviewForSend` filtered on `content ||
    // t` and the composer's Send affordance matched it, while Done asked only
    // for `content` — so ✓ Done on a round of walkthrough marks lit the button,
    // disarmed the mode, and left the notes sitting there unsent (Bugbot,
    // PR #1074). `isSendable` is now the only spelling of the test.
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "", t: 12.5 });
    await r.machine.done();
    expect(r.log.filter((l) => l === "submit")).toHaveLength(1);
    expect(modeOf(r)).toBe("off");
  });

  test("a stamped note ALREADY SENT is not sent again by Done", async () => {
    const r = rig();
    r.machine.set(true);
    const note = r.store.add({ content: "", t: 3 });
    r.store.markSent([note]);
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

  test("Done is NOT the walkthrough's exit: refused while it records and while it settles", async () => {
    // The marks a walkthrough's clicks leave are stamped and WORDLESS until the
    // transcript lands, and `isSendable` counts them as messages — so a Done
    // reaching this door mid-settle auto-submitted them empty and disarmed the
    // mode while the transcription was still on its way (Bugbot, PR #1074).
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "", t: 4.5 });
    r.rec.on = true; // the recording, and the mic prompt's own window with it
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(modeOf(r)).toBe("recording");

    // Through Stopping…/Transcribing… the recorder's own flag is already down —
    // the phase is what says the marks are still the recording's.
    r.beginSettle();
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(modeOf(r)).toBe("settling");
    r.finishSettle("transcribing");
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(modeOf(r)).toBe("transcribing");
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

  // ── the settle read off the RECORDER, not off the echo (Bugbot, PR #1074) ──
  //
  // `setPhase` is fed by a React effect over `recSnap`, so between the
  // recorder's flags moving and that effect running there is one commit in
  // which `recording()` is already false and `phase` is still null. Every one
  // of these four assertions FAILED in that window before `settlePhase()`: the
  // machine fell back to `comment` and the typed round's doors opened on a
  // walkthrough's marks.

  test("the unechoed settle still reads as a settle, not as comment", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.beginSettleUnechoed();
    expect(modeOf(r)).toBe("settling");
    expect(walkthroughOwns(modeOf(r))).toBe(true);
    // …and the echo, when it lands, is the FINER answer and takes over.
    r.machine.setPhase("transcribing");
    expect(modeOf(r)).toBe("transcribing");
  });

  test("the trash in the unechoed settle does NOT throw the recording's marks", () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "a mark", createdAt: 6000 });
    r.rec.on = true;
    r.beginSettleUnechoed();
    r.machine.discard();
    // THE DEFECT: `notesDiscard` used to delete the round and disarm here,
    // while the transcription it never asked to stop went on to land and
    // auto-send words with nothing left to anchor them to.
    expect(r.store.list()).toHaveLength(1);
    expect(r.rec.discarded).toBe(0);
    expect(r.machine.armed()).toBe(true);
    expect(modeOf(r)).toBe("settling");
  });

  test("✓ Done refuses in the unechoed settle, and the nav lock holds", async () => {
    const r = rig();
    r.machine.set(true);
    r.store.add({ content: "a mark", createdAt: 6000 });
    r.rec.on = true;
    r.machine.setBusyHold(true);
    r.beginSettleUnechoed();
    // The lock is the MODE's claim and `hold` is only half of it: the other
    // half used to be the stale `phase`, so the lock dropped in this window too.
    expect(r.machine.locked()).toBe(true);
    await r.machine.done();
    expect(r.log).not.toContain("submit");
    expect(r.machine.armed()).toBe(true);
  });

  test("arriving at the narrow chat view disarms in the unechoed settle too", () => {
    const r = rig();
    r.machine.set(true);
    r.rec.on = true;
    r.beginSettleUnechoed();
    r.machine.arriveNarrowChat();
    expect(r.machine.armed()).toBe(false);
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

describe("walkthroughOwns — one spelling of \"are this mark's words still coming\"", () => {
  test("every state but off and comment belongs to the recorder", () => {
    // The seats read it (`seatsAria`), the two doors that refuse read it
    // (`done`, and `notesDiscard`'s own phase test), and so does the send
    // (`isSendableNow`). Three private `recording()` tests were three chances
    // to disagree about the SETTLE, which is where every one of PR #1074's
    // walkthrough bugs lived.
    expect(walkthroughOwns("off")).toBe(false);
    expect(walkthroughOwns("comment")).toBe(false);
    expect(walkthroughOwns("recording")).toBe(true);
    expect(walkthroughOwns("settling")).toBe(true);
    expect(walkthroughOwns("transcribing")).toBe(true);
  });
});

describe("forceOff — the teardown's disarm (T:8797, PR3 review #1)", () => {
  test("it ABANDONS a live recording; it never ends one", () => {
    // `set(false)` ends a recording with `end()`, which continues into
    // `transcribe` → `deliver` → the automatic send. The hosted teardown also
    // runs on a React unmount, so that send landed in a chat that no longer
    // existed — and the `abandon()` seam added to stop it was unreachable,
    // because by the time the teardown asked, the recorder was already
    // settling. This door is the one that reaches it.
    const r = rig({ params: { annmode: "1" } });
    r.machine.set(true);
    r.rec.on = true;
    expect(r.machine.mode()).toBe("recording");
    r.log.length = 0;

    r.machine.forceOff();

    expect(r.rec.abandoned).toBe(1);
    expect(r.rec.ended).toBe(0);
    expect(r.rec.discarded).toBe(0);
    expect(r.log).not.toContain("submit");
  });

  test("it is T:8797's bare `annOn = false` — no param, no repaint, no composer", () => {
    // T's first teardown line and nothing more: a URL write, a `render()` and a
    // `closeComposer()` on the way down are all things T does not do while the
    // document is unloading (PR3 review #5).
    const r = rig({ params: { annmode: "1" } });
    r.machine.set(true);
    r.composer.open = true;
    r.log.length = 0;

    r.machine.forceOff();

    expect(r.params.get("annmode")).toBe("1");
    expect(r.log).not.toContain("render");
    expect(r.log).not.toContain("close");
    expect(r.log).not.toContain("tool:false");
    // What it DOES do: the state goes where the DOM already is, and the lock
    // that state was holding is handed back.
    expect(r.machine.armed()).toBe(false);
    expect(r.machine.mode()).toBe("off");
    expect(r.machine.locked()).toBe(false);
    expect(r.log).toContain("lock:false");
  });

  test("the settle's hold goes with it, and a second call is a no-op", () => {
    const r = rig();
    r.machine.set(true);
    // THE RECORDER IS GENUINELY SETTLING (R1-final-b). This drove
    // `machine.setPhase("transcribing")` alone, which moves the React ECHO and
    // nothing else — the rig's `settling()` reads the recorder's own phase, so
    // it answered false and the door under test was the plain `phase` door
    // rather than the settle. `beginSettle` + `finishSettle` are the real
    // recorder's order (flags down before the await, then the status and the
    // lock claim), and they move both.
    r.beginSettle();
    r.finishSettle("transcribing");
    expect(r.recorder.settling()).toBe(true);
    expect(r.machine.mode()).toBe("transcribing");
    expect(r.machine.locked()).toBe(true);

    r.machine.forceOff();
    // WHAT THE TEARDOWN OWNS is handed back: the layer, the hold, and the lock.
    expect(r.machine.armed()).toBe(false);
    expect(r.machine.busyHold()).toBe(false);
    expect(r.machine.locked()).toBe(false);
    // AND WHAT IT DOES NOT OWN is left alone, which is the answer residual (b)
    // was hiding. `mode()` still names the settle, because `settlePhase()` falls
    // through to the RECORDER's own synchronous answer and the recorder is
    // genuinely still settling: `abandon()` cancels the settle's RESULT (no
    // assign, no deliver, no auto-send — `ann/rec.test.ts`) and deliberately
    // paints no status over a document that is going away, so its awaits are
    // still out. The old assertion read "off" here only because the rig's
    // `settling()` was answering false to a phase nothing had given it. Nothing
    // hangs off this: the lock above is what the rest of the app reads, and the
    // stale ender's own gated writes return the state to `off` when the words
    // land.
    expect(r.machine.mode()).toBe("settling");

    // A second call reaches the recorder's own guard and the lock, and nothing
    // else: no `end()`, no send, no repaint. (`abandon()` is called
    // unconditionally — the recorder is the one place that knows whether there
    // is anything live to stop, and this rig's stub does not self-guard the way
    // `ann/rec.ts` does.)
    r.log.length = 0;
    r.machine.forceOff();
    expect(r.log).toEqual(["rec.abandon", "lock:false"]);
    expect(r.rec.ended).toBe(0);
  });
});
