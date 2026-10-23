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
  test("caps apps at 6, canvases at 4 and pins at 3, keeping the head of each list in order", () => {
    const desk = [...range(9).map(app), ...range(6).map(canvas)];
    const out = railExtras({ desk, pins: range(5).map(pin), bot: false, canvases: true });
    expect(RAIL_APPS_MAX).toBe(6);
    expect(RAIL_CANVASES_MAX).toBe(4);
    expect(RAIL_PINS_MAX).toBe(3);
    expect(out.apps.map((a) => a.name)).toEqual(range(6).map((i) => `app${i}`));
    expect(out.canvases.map((a) => a.name)).toEqual(range(4).map((i) => `canvas${i}`));
    expect(out.pins.map((b) => b.id)).toEqual(["b0", "b1", "b2"]);
  });

  test("splits an interleaved desk on `canvas`, each half in its own order", () => {
    const out = railExtras({
      desk: [canvas(2), app(2), app(1), canvas(1)],
      pins: [],
      bot: false,
      canvases: true,
    });
    expect(out.apps.map((a) => a.name)).toEqual(["app2", "app1"]);
    expect(out.canvases.map((a) => a.name)).toEqual(["canvas2", "canvas1"]);
    expect(out.pins).toEqual([]);
  });

  test("the Canvases gate off drops the canvases group only", () => {
    const out = railExtras({ desk: [app(1), canvas(1)], pins: [pin(1)], bot: false, canvases: false });
    expect(out.apps.map((a) => a.name)).toEqual(["app1"]);
    expect(out.canvases).toEqual([]);
    expect(out.pins.map((b) => b.id)).toEqual(["b1"]);
  });

  test("Fused Bot shows no group", () => {
    const out = railExtras({ desk: [app(1), canvas(1)], pins: [pin(1)], bot: true, canvases: true });
    expect(out).toEqual({ apps: [], canvases: [], pins: [] });
  });
});
