// railExtras (sidebar-rail-lib.ts): the collapsed rail's app, canvas and
// pinned-bookmark groups — caps, order, the split and the gates.
import { describe, expect, test } from "bun:test";
import type { Bookmark } from "@platform/lib/bookmarks";
import type { CurrentApp } from "@shell/current-apps-lib";
import {
  RAIL_APPS_MAX,
  RAIL_CANVASES_MAX,
  RAIL_PINS_MAX,
  railExtras,
} from "@shell/sidebar-rail-lib";

const app = (n: number): CurrentApp => ({
  path: `/w/app${n}`,
  name: `app${n}`,
  entry: null,
  kind: "workspace",
  canvas: null,
  exists: true,
  running: false,
  queued: 0,
  unread: false,
  iconUrl: null,
});

const canvas = (n: number): CurrentApp => ({
  ...app(100 + n),
  path: `/w/canvas${n}`,
  name: `canvas${n}`,
  canvas: { env: "prod", name: `canvas${n}` },
});

const pin = (n: number): Bookmark => ({
  id: `b${n}`,
  name: `b${n}`,
  url: `/explorer/view/b${n}`,
  created_at: 0,
  pinned: true,
});

const range = (n: number): number[] => Array.from({ length: n }, (_, i) => i);

describe("railExtras", () => {
  test("caps apps, canvases and pins at 7 each, keeping the head of each list in order", () => {
    // Seven of each (owner, 2026-11-09: was 5 / 5 / 3).
    const desk = [...range(9).map(app), ...range(9).map(canvas)];
    const out = railExtras({ desk, pins: range(9).map(pin), canvases: true });
    expect(RAIL_APPS_MAX).toBe(7);
    expect(RAIL_CANVASES_MAX).toBe(7);
    expect(RAIL_PINS_MAX).toBe(7);
    expect(out.apps.map((a) => a.name)).toEqual(range(7).map((i) => `app${i}`));
    expect(out.canvases.map((a) => a.name)).toEqual(range(7).map((i) => `canvas${i}`));
    expect(out.pins.map((b) => b.id)).toEqual(range(7).map((i) => `b${i}`));
  });

  test("splits an interleaved desk on `canvas`, each half in its own order", () => {
    const out = railExtras({
      desk: [canvas(2), app(2), app(1), canvas(1)],
      pins: [],
      canvases: true,
    });
    expect(out.apps.map((a) => a.name)).toEqual(["app2", "app1"]);
    expect(out.canvases.map((a) => a.name)).toEqual(["canvas2", "canvas1"]);
    expect(out.pins).toEqual([]);
  });

  test("the Canvases gate off drops the canvases group only", () => {
    const out = railExtras({ desk: [app(1), canvas(1)], pins: [pin(1)], canvases: false });
    expect(out.apps.map((a) => a.name)).toEqual(["app1"]);
    expect(out.canvases).toEqual([]);
    expect(out.pins.map((b) => b.id)).toEqual(["b1"]);
  });
});
