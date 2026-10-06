// WHICH APP IS THIS BUILD WEARING — Fused Render or Fused Bot. The two macOS
// apps are built from this one repo (fused_render/flavor.py is the server-side
// twin, baked at package time); the shell asks here instead of spelling
// "Fused Render" or "fused-render://" anywhere it would show or dial it.
//
// A SYNCHRONOUS store, seeded once from `/api/config` in main.tsx BEFORE the
// first render: the sidebar decides its rows and the document its title at
// that render, and a value that arrived a tick later would be a flash of the
// wrong brand. Unseeded (an older server, a test) it reads as Render — the
// defaults below are exactly what the shell said before flavors existed.
import type { Config } from "@platform/lib/api";

export type Flavor = "render" | "bot";

interface FlavorState {
  flavor: Flavor;
  scheme: string;
  displayName: string;
}

const RENDER: FlavorState = {
  flavor: "render",
  scheme: "fused-render",
  displayName: "Fused Render",
};

let state: FlavorState = RENDER;

/** Seed from the config payload (main.tsx, once per load). Partial payloads
 *  fill in from the Render defaults, field by field: a `flavor: "bot"` with
 *  no `scheme` beside it is a server bug, not a reason to dial nowhere. */
export function seedFlavor(config: Pick<Config, "flavor" | "scheme" | "display_name">): void {
  const flavor: Flavor = config.flavor === "bot" ? "bot" : "render";
  state = {
    flavor,
    scheme: config.scheme || (flavor === "bot" ? "fused-bot" : RENDER.scheme),
    displayName: config.display_name || (flavor === "bot" ? "Fused Bot" : RENDER.displayName),
  };
}

export function flavor(): Flavor {
  return state.flavor;
}

export function isBot(): boolean {
  return state.flavor === "bot";
}

/** "Fused Render" / "Fused Bot" — the brand as copy shows it. */
export function displayName(): string {
  return state.displayName;
}

/** The bundle's name as macOS panes list it (System Settings › Full Disk
 *  Access): "FusedRender" / "FusedBot", no space. */
export function bundleName(): string {
  return state.displayName.replace(/\s+/g, "");
}

/** The custom URL scheme the running app answers — `fused-render` /
 *  `fused-bot` (fused_render/deeplink.py). */
export function deepLinkScheme(): string {
  return state.scheme;
}
