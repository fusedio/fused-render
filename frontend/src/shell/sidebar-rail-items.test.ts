// railExtras (sidebar-rail-lib.ts): the collapsed rail's app and pinned-
// bookmark groups — caps, order, and the Fused Bot gate.
import { describe, expect, test } from "bun:test";
import type { Bookmark } from "@platform/lib/bookmarks";
import type { CurrentApp } from "@shell/current-apps-lib";
import { RAIL_APPS_MAX, RAIL_PINS_MAX, railExtras } from "@shell/sidebar-rail-lib";

const app = (n: number): CurrentApp => ({
  path: `/w/app${n}`,
  name: `app${n}`,
  entry: null,
  kind: "workspace",
  exists: true,
  running: false,
  queued: 0,
  unread: false,
  iconUrl: null,
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
  test("caps apps at 10 and pins at 3, keeping the head of each list in order", () => {
    const out = railExtras({ apps: range(14).map(app), pins: range(5).map(pin), bot: false });
    expect(RAIL_APPS_MAX).toBe(10);
    expect(RAIL_PINS_MAX).toBe(3);
    expect(out.apps.map((a) => a.name)).toEqual(range(10).map((i) => `app${i}`));
    expect(out.pins.map((b) => b.id)).toEqual(["b0", "b1", "b2"]);
  });

  test("short lists pass through whole; empty stays empty", () => {
    const out = railExtras({ apps: [app(2), app(1)], pins: [], bot: false });
    expect(out.apps.map((a) => a.name)).toEqual(["app2", "app1"]);
    expect(out.pins).toEqual([]);
  });

  test("Fused Bot shows neither group", () => {
    const out = railExtras({ apps: [app(1)], pins: [pin(1)], bot: true });
    expect(out).toEqual({ apps: [], pins: [] });
  });
});
