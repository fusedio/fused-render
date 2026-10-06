// Starter apps (OpenBot apps.js starterBadge / renderStarters / loadStarters), the non-React half: the apps that ship
// with fused-render (GET /api/bot-apps/starters). The strip above the gallery is a catalog of what is NOT installed yet,
// one Install button each (StartersStrip.tsx). Once installed a starter is an app like any other: it leaves the strip
// and its gallery card (AppsPanel.tsx Card) carries the starter state instead: the badge that says whether the app's
// one-time setup is done (its *_status tool, read by GET /api/bot-apps/starters/status after the panel is drawn) and an
// Update button when a newer version ships (confirmed: a build may have edited the installed copy).
import type { Starter } from "../lib/api";

/** Where OpenBot's strings say "OpenBot": the product the starters ship with here. */
export const SHIPS_WITH = "Fused Render";

/** A starter as the panel holds it: `ready` is null until the status call says otherwise (or when it cannot tell). */
export type StarterRow = Starter & { ready: boolean | null };

export const starterRows = (list: Starter[] | null | undefined): StarterRow[] => (list || []).map((s) => ({ ...s, ready: null }));

/** The strip's rows: the starters that are not installed yet. */
export const toInstall = (rows: StarterRow[]): StarterRow[] => rows.filter((s) => !s.installed);

/** The installed starter a gallery card stands for (matched on the app folder), else null. */
export const starterOf = (rows: StarterRow[], dir: string): StarterRow | null =>
  rows.find((s) => s.installed && !!s.dir && s.dir === dir) || null;

/** The status call is slow: only make it when some installed starter has a setup tool. */
export const needsStatus = (rows: StarterRow[]): boolean => rows.some((s) => s.installed && !!s.setup_tool);

/** Fold the status reply in: only keys it reports change. */
export const withStatus = (rows: StarterRow[], ready: Record<string, boolean | null> | null | undefined): StarterRow[] =>
  rows.map((s) => (ready && s.key in ready ? { ...s, ready: ready[s.key] } : s));

/** The state badge on an installed starter's card: Needs setup / Ready from the status tool; otherwise Installed. */
export function starterBadge(s: Pick<StarterRow, "installed" | "ready">): { text: string; cls: string; title?: string } | null {
  if (!s.installed) return null;
  if (s.ready === false) return { text: "Needs setup", cls: "badge setup", title: "Open the app once and finish its setup (the Google apps need a service-account key)" };
  if (s.ready === true) return { text: "Ready", cls: "badge ready" };
  return { text: "Installed", cls: "badge" };
}

export type StarterKind = "install" | "update";

/** The card's Update button, only when a newer version ships than the one installed. */
export function updateButton(s: Pick<StarterRow, "installed" | "update" | "version">): { label: string; title: string } | null {
  if (!s.installed || !s.update) return null;
  return { label: "Update", title: `Replace the installed files with the version that ships with ${SHIPS_WITH} (v${s.version})` };
}

/** The busy label while an install / update runs. */
export const busyLabel = (kind: StarterKind): string => (kind === "update" ? "Updating…" : "Installing…");

/** The Update confirm (askConfirm title, text). */
export const updateConfirm = (s: Pick<StarterRow, "name" | "dir" | "version">): [string, string] => [
  `Update ${s.name}?`,
  `This replaces the app's files under ${s.dir} with the ones that ship with ${SHIPS_WITH} (v${s.version}). Edits a build made to that copy are lost; its saved key and library under .fused/ stay.`,
];
