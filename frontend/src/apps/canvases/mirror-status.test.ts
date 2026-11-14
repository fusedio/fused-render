// The reference-canvases chip and the folder-layout notice, as the Canvases
// page words them (mirror-status.ts): every mirror state and every reason no
// run would start, the count / relative-time / progress formatting, the
// refusals of an on-demand run, and how often the status is re-read.
import { afterEach, beforeEach, describe, expect, setSystemTime, test } from "bun:test";
import type { LayoutPending, MirrorSkipReason, MirrorState, MirrorSummary } from "./api";
import {
  MIRROR_POLL_IDLE_MS,
  MIRROR_POLL_RUNNING_MS,
  layoutNotice,
  mirrorChip,
  mirrorPollDelay,
  mirrorReadIsCurrent,
  mirrorRunRefusal,
  mirroredLine,
  referenceCanvasCount,
  runningLine,
} from "./mirror-status";

const NOW = Date.UTC(2026, 10, 14, 12, 0, 0);
const nowS = NOW / 1000;

beforeEach(() => setSystemTime(new Date(NOW)));
afterEach(() => setSystemTime());

const summary = (over: Partial<MirrorSummary> = {}): MirrorSummary => ({
  state: "ok",
  last_state: "ok",
  done: 0,
  total: 0,
  finished_at: nowS - 12 * 60,
  mirrored: 340,
  error: null,
  eligible: true,
  skip_reason: null,
  ...over,
});

describe("formatting", () => {
  test("the count is singular for one and grouped past a thousand", () => {
    expect(referenceCanvasCount(0)).toBe("0 reference canvases");
    expect(referenceCanvasCount(1)).toBe("1 reference canvas");
    expect(referenceCanvasCount(340)).toBe("340 reference canvases");
    expect(referenceCanvasCount(1240)).toBe("1,240 reference canvases");
  });

  test("the stamp is the shared relative time, and is dropped when there is none", () => {
    expect(mirroredLine(340, nowS - 12 * 60)).toBe("340 reference canvases · updated 12m ago");
    expect(mirroredLine(340, nowS - 5)).toBe("340 reference canvases · updated just now");
    expect(mirroredLine(340, nowS - 3 * 3600)).toBe("340 reference canvases · updated 3h ago");
    expect(mirroredLine(1, nowS - 2 * 86400)).toBe("1 reference canvas · updated 2d ago");
    expect(mirroredLine(340, null)).toBe("340 reference canvases");
  });

  test("progress shows counts only once the total is known", () => {
    expect(runningLine(57, 340)).toBe("Updating reference canvases… 57 of 340");
    expect(runningLine(0, 340)).toBe("Updating reference canvases… 0 of 340");
    expect(runningLine(0, 0)).toBe("Updating reference canvases…");
    expect(runningLine(3, 0)).toBe("Updating reference canvases…");
    expect(runningLine(1200, 2400)).toBe("Updating reference canvases… 1,200 of 2,400");
    // Never "341 of 340".
    expect(runningLine(341, 340)).toBe("Updating reference canvases… 340 of 340");
  });
});

describe("mirrorChip, by state", () => {
  test("no summary, no chip", () => {
    expect(mirrorChip(null)).toBeNull();
    expect(mirrorChip(undefined)).toBeNull();
  });

  test("running: live counts, not pressable", () => {
    const chip = mirrorChip(summary({ state: "running", done: 57, total: 340 }));
    expect(chip?.text).toBe("Updating reference canvases… 57 of 340");
    expect(chip?.tone).toBe("running");
    expect(chip?.canRun).toBe(false);
  });

  test("running before the listing: no counts", () => {
    const chip = mirrorChip(summary({ state: "running", last_state: "idle", finished_at: null, mirrored: 0 }));
    expect(chip?.text).toBe("Updating reference canvases…");
  });

  test("busy: running without counts, whatever the counts say", () => {
    const chip = mirrorChip(summary({ state: "busy", last_state: "busy", done: 4, total: 9 }));
    expect(chip?.text).toBe("Updating reference canvases…");
    expect(chip?.tone).toBe("running");
  });

  test("ok: count and age, pressable", () => {
    const chip = mirrorChip(summary());
    expect(chip?.text).toBe("340 reference canvases · updated 12m ago");
    expect(chip?.tone).toBe("ok");
    expect(chip?.canRun).toBe(true);
  });

  test("partial: the ok line, a warning tone, the error on hover", () => {
    const chip = mirrorChip(
      summary({ state: "partial", last_state: "partial", error: "3 canvases could not be pulled" }),
    );
    expect(chip?.text).toBe("340 reference canvases · updated 12m ago");
    expect(chip?.tone).toBe("warning");
    expect(chip?.title).toContain("3 canvases could not be pulled");
    expect(chip?.canRun).toBe(true);
  });

  test("partial with no message still explains the warning", () => {
    const chip = mirrorChip(summary({ state: "partial", last_state: "partial" }));
    expect(chip?.title).toContain("the last update was incomplete");
  });

  test("failed: a short error, the message on hover, pressable to retry", () => {
    const chip = mirrorChip(
      summary({ state: "failed", last_state: "failed", error: "fused canvas mirror failed" }),
    );
    expect(chip?.text).toBe("Reference canvases could not be updated");
    expect(chip?.tone).toBe("error");
    expect(chip?.title).toContain("fused canvas mirror failed");
    expect(chip?.canRun).toBe(true);
  });

  test("auth: says to sign in again", () => {
    const chip = mirrorChip(
      summary({ state: "auth", last_state: "auth", error: "sign in to Fused again" }),
    );
    expect(chip?.text).toBe("Sign in again to update reference canvases");
    expect(chip?.tone).toBe("error");
    expect(chip?.title).toContain("sign in to Fused again");
  });

  test("unsupported: said, and not pressable — a run cannot help", () => {
    // The server reports it as both the state and the reason no run starts.
    const chip = mirrorChip(
      summary({ state: "unsupported", last_state: "unsupported", eligible: false, skip_reason: "unsupported" }),
    );
    expect(chip?.text).toBe("Reference canvases need a newer Fused");
    expect(chip?.tone).toBe("muted");
    expect(chip?.canRun).toBe(false);
  });

  test("no_cli: nothing", () => {
    expect(
      mirrorChip(summary({ state: "no_cli", last_state: "no_cli", eligible: false, skip_reason: "no_cli" })),
    ).toBeNull();
  });

  test("idle: nothing until a run has finished", () => {
    expect(
      mirrorChip(summary({ state: "idle", last_state: "idle", finished_at: null, mirrored: 0 })),
    ).toBeNull();
  });

  test("a state this page does not know: nothing", () => {
    expect(mirrorChip(summary({ state: "later" as MirrorState }))).toBeNull();
  });

  test("every known state is decided", () => {
    const shown: Record<MirrorState, boolean> = {
      idle: false,
      running: true,
      ok: true,
      partial: true,
      failed: true,
      unsupported: true,
      auth: true,
      no_cli: false,
      busy: true,
    };
    for (const [state, visible] of Object.entries(shown)) {
      const chip = mirrorChip(summary({ state: state as MirrorState }));
      expect(chip !== null).toBe(visible);
    }
  });
});

describe("mirrorChip, by the reason no run would start", () => {
  const hidden: MirrorSkipReason[] = [
    "signed_out",
    "not_in_use",
    "env_locked",
    "not_configured",
    "unknown_env",
    "no_cli",
  ];
  for (const skip_reason of hidden) {
    test(`${skip_reason}: nothing, even with copies from an earlier run`, () => {
      expect(mirrorChip(summary({ eligible: false, skip_reason }))).toBeNull();
      expect(
        mirrorChip(summary({ state: "failed", last_state: "failed", error: "x", eligible: false, skip_reason })),
      ).toBeNull();
      expect(mirrorChip(summary({ state: "idle", last_state: "idle", eligible: false, skip_reason }))).toBeNull();
    });
  }

  test("unsupported: the one reason that is said", () => {
    expect(mirrorChip(summary({ eligible: false, skip_reason: "unsupported" }))?.text).toBe(
      "Reference canvases need a newer Fused",
    );
  });
});

describe("mirrorRunRefusal", () => {
  test("a started run, one already running and a held one are not errors", () => {
    expect(mirrorRunRefusal(null)).toBeNull();
    expect(mirrorRunRefusal("running")).toBeNull();
    expect(mirrorRunRefusal("held")).toBeNull();
  });

  test("every other reason is a sentence", () => {
    for (const reason of [
      "no_cli",
      "unsupported",
      "signed_out",
      "not_configured",
      "env_locked",
      "shutting_down",
      "error",
      "something_new",
    ]) {
      const said = mirrorRunRefusal(reason);
      expect(said).toBeTruthy();
      expect(said).toMatch(/^[A-Z].*\.$/);
    }
    expect(mirrorRunRefusal("signed_out")).toBe("Sign in to Fused to update reference canvases.");
    expect(mirrorRunRefusal("unsupported")).toBe("Reference canvases need a newer Fused.");
  });
});

describe("layoutNotice", () => {
  const layout = (over: Partial<LayoutPending> = {}): LayoutPending => ({
    deferred: {},
    conflicts: {},
    blocked: "",
    error: "",
    ...over,
  });

  test("nothing pending, or nothing known: no notice", () => {
    expect(layoutNotice(null)).toBeNull();
    expect(layoutNotice(undefined)).toBeNull();
    expect(layoutNotice(layout())).toBeNull();
  });

  test("a canvas whose chat is still open needs no notice", () => {
    expect(layoutNotice(layout({ deferred: { demo: "its chat is still open" } }))).toBeNull();
  });

  test("conflicts are counted and listed by name", () => {
    const one = layoutNotice(layout({ conflicts: { zeta: "something else is at user/zeta" } }));
    expect(one?.summary).toBe("1 existing canvas could not be moved to the new folder layout");
    const two = layoutNotice(layout({ conflicts: { zeta: "z", alpha: "a" }, deferred: { mid: "m" } }));
    expect(two?.summary).toBe("2 existing canvases could not be moved to the new folder layout");
    expect(two?.conflicts).toEqual([
      { name: "alpha", why: "a" },
      { name: "zeta", why: "z" },
    ]);
    expect(two?.detail).toBeNull();
  });

  test("a blocked or failed pass is a notice by itself", () => {
    const blocked = layoutNotice(layout({ blocked: "canvas 'user' must move first" }));
    expect(blocked?.summary).toBe("Existing canvases could not be moved to the new folder layout");
    expect(blocked?.detail).toBe("canvas 'user' must move first");
    const failed = layoutNotice(layout({ error: "the canvas folders could not be checked", blocked: "b" }));
    expect(failed?.detail).toBe("the canvas folders could not be checked");
  });
});

describe("mirrorPollDelay", () => {
  test("closely while a run is in flight, slowly otherwise", () => {
    expect(mirrorPollDelay(summary({ state: "running" }))).toBe(MIRROR_POLL_RUNNING_MS);
    expect(mirrorPollDelay(summary())).toBe(MIRROR_POLL_IDLE_MS);
    expect(mirrorPollDelay(summary({ state: "idle", last_state: "idle" }))).toBe(MIRROR_POLL_IDLE_MS);
    expect(mirrorPollDelay(summary({ state: "busy" }))).toBe(MIRROR_POLL_IDLE_MS);
  });

  test("never for an environment where no run would start", () => {
    expect(mirrorPollDelay(null)).toBeNull();
    expect(mirrorPollDelay(summary({ eligible: false, skip_reason: "signed_out" }))).toBeNull();
    expect(mirrorPollDelay(summary({ state: "no_cli", eligible: false, skip_reason: "no_cli" }))).toBeNull();
    expect(
      mirrorPollDelay(summary({ state: "unsupported", eligible: false, skip_reason: "unsupported" })),
    ).toBeNull();
  });
});

describe("mirrorReadIsCurrent", () => {
  const seedA = { name: "page status A" };
  const seedB = { name: "page status B" };

  test("a read with nothing newer since it was sent is shown", () => {
    expect(mirrorReadIsCurrent({ gen: 0, seed: seedA }, 0, seedA)).toBe(true);
    expect(mirrorReadIsCurrent({ gen: 3, seed: null }, 3, null)).toBe(true);
  });

  test("a read sent before a newer page status is dropped", () => {
    expect(mirrorReadIsCurrent({ gen: 0, seed: seedA }, 0, seedB)).toBe(false);
    expect(mirrorReadIsCurrent({ gen: 0, seed: null }, 0, seedA)).toBe(false);
  });

  test("a read sent before a refresh the user asked for is dropped", () => {
    expect(mirrorReadIsCurrent({ gen: 0, seed: seedA }, 1, seedA)).toBe(false);
    expect(mirrorReadIsCurrent({ gen: 1, seed: seedA }, 2, seedA)).toBe(false);
  });
});
