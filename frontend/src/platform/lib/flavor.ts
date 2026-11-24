// THE APP'S NAMES — the shell asks here instead of spelling "Fused Render" or
// "fused-render://" anywhere it would show or dial it (fused_render/_flavor.py
// is the server-side twin).
//
// A SYNCHRONOUS store, seeded once from `/api/config` in main.tsx BEFORE the
// first render: the document title and copy are decided at that render, and a
// value that arrived a tick later would be a flash of the wrong name.
// Unseeded (an older server, a test) it reads as the defaults below.
import type { Config } from "@platform/lib/api";

interface FlavorState {
  scheme: string;
  displayName: string;
  bundleName: string;
}

const DEFAULTS: FlavorState = {
  scheme: "fused-render",
  displayName: "Fused Render",
  bundleName: "Fused Render",
};

let state: FlavorState = DEFAULTS;

/** Seed from the config payload (main.tsx, once per load). Missing fields
 *  fall back to the defaults, field by field. */
export function seedFlavor(config: Pick<Config, "scheme" | "display_name" | "bundle_name">): void {
  state = {
    scheme: config.scheme || DEFAULTS.scheme,
    displayName: config.display_name || DEFAULTS.displayName,
    bundleName: config.bundle_name || DEFAULTS.bundleName,
  };
}

/** "Fused Render" — the brand as copy shows it. */
export function displayName(): string {
  return state.displayName;
}

/** The bundle's name as Finder and macOS panes list it (System Settings ›
 *  Full Disk Access): the .app folder the server runs from — "Fused Render",
 *  or "FusedRender" on an install from before the rename that no update has
 *  moved yet. */
export function bundleName(): string {
  return state.bundleName;
}

/** The state home — `~/.fused-render` (fused_render/_flavor.py). Prefs,
 *  template registry, staged core templates live here, never under the
 *  installation. */
export function userDataDir(): string {
  return "~/.fused-render";
}

/** The custom URL scheme the running app answers — `fused-render`
 *  (fused_render/deeplink.py). */
export function deepLinkScheme(): string {
  return state.scheme;
}
