// The backdrop grace: a press on the scrim in the dialog's first moments is the
// second half of a double-click on whatever opened it, not a dismissal
// (dirty-guard.ts `backdropPressCloses`; Sina, 2026-09-19). Rules here, and the
// source assertion that Modal.tsx's scrim actually asks them — the same
// no-DOM shape modal-dirty-guard.test.ts uses.
import { beforeAll, describe, expect, test } from "bun:test";
import { BACKDROP_GRACE_MS, backdropPressCloses } from "./dirty-guard";

let modal: string;

beforeAll(async () => {
  modal = await Bun.file(new URL("./Modal.tsx", import.meta.url).pathname).text();
});

describe("backdropPressCloses", () => {
  test("a press inside the grace window is ignored", () => {
    expect(backdropPressCloses(1000, 1000)).toBe(false);
    expect(backdropPressCloses(1000, 1000 + 150)).toBe(false);
    expect(backdropPressCloses(1000, 1000 + BACKDROP_GRACE_MS - 1)).toBe(false);
  });

  test("a press at or after the grace window closes", () => {
    expect(backdropPressCloses(1000, 1000 + BACKDROP_GRACE_MS)).toBe(true);
    expect(backdropPressCloses(1000, 1000 + 5000)).toBe(true);
  });

  test("the window outlasts any double-click", () => {
    // macOS's slowest double-click setting is about half a second; a second
    // press inside that is the habit this exists for, never a dismissal.
    expect(BACKDROP_GRACE_MS).toBeGreaterThanOrEqual(500);
    // …and is short enough that a deliberate press-out after reading the
    // dialog still works without the user noticing a dead zone.
    expect(BACKDROP_GRACE_MS).toBeLessThanOrEqual(800);
  });
});

describe("the scrim asks the rule", () => {
  test("Modal's overlay mousedown consults backdropPressCloses with the open time", () => {
    expect(modal).toContain("const openedAt = useRef(performance.now())");
    expect(modal).toContain("backdropPressCloses(openedAt.current, performance.now())");
    // The guard sits between the target check and attemptClose: a press on the
    // dialog body is still not a close, and a press on the scrim goes through
    // the grace rule before it can be one.
    const i = modal.indexOf("if (e.target !== e.currentTarget) return;");
    const j = modal.indexOf("backdropPressCloses(openedAt.current");
    const k = modal.indexOf("attemptClose();", j);
    expect(i).toBeGreaterThan(-1);
    expect(j).toBeGreaterThan(i);
    expect(k).toBeGreaterThan(j);
  });

  test("the other closers are untouched", () => {
    // Escape and ✕ still call attemptClose directly — the grace is the scrim's
    // alone, because only the scrim can be hit from where the pointer already was.
    expect(modal).toContain("onClick={attemptClose}");
    expect(modal).toMatch(/if \(!isTopmost\(token\.current\)\) return;\s*attemptClose\(\);/);
  });
});
