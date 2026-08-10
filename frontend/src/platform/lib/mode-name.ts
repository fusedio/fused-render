// Display name for a template-mode key. Split out of ModeSwitcher.tsx when the
// mode control grew a visible LABEL (the shared ModeMenu shows icon + name):
// while the name only ever reached a tooltip, a bare capitalize was good
// enough; on screen, `Claude_split` is a defect. Pure and dependency-free so
// it can be unit-tested (mode-name.test.ts) without a DOM.

// Modes the shell renders with no template folder (SPEC PT-12/D81) — their
// keys are internal sentinels and must never be shown as typed.
const SENTINEL_NAMES: Record<string, string> = {
  _render: "Rendered",
  _listing: "Listing",
  // Pane-only sentinel (ListingPreviewPane). "Preview", like the `app` mode
  // below and for the same reason — the two are the same thing seen from the
  // pane and from the app route, and they must not disagree on screen.
  _app: "Preview",
};

// Registry keys whose conventional casing a generic capitalize would destroy.
// Deliberately a short list of the ones that actually ship: an unknown key
// falls through to the humanizer rather than being guessed at.
const NICE_NAMES: Record<string, string> = {
  // Not "App": by the time this label is on screen the user is already inside
  // the app, so naming the mode after the app says nothing — it is the app's
  // own rendered preview, as opposed to its history (History) or its build
  // surface (Claude split). "Preview" also names it the way the rest of the
  // shell talks about this surface (the preview pane, the preview header).
  // The KEY stays `app` everywhere it is not a display string: the registry,
  // `?_mode=app`, APP_OPEN_MODE, and the "Open as app" / "Add as app"
  // buttons, which are about the app concept, not this view.
  app: "Preview",
  duckdb: "DuckDB",
  geojson: "GeoJSON",
  json: "JSON",
  csv: "CSV",
  html: "HTML",
  sql: "SQL",
  pdf: "PDF",
};

export function modeTitle(mode: string): string {
  const sentinel = SENTINEL_NAMES[mode];
  if (sentinel) return sentinel;
  const nice = NICE_NAMES[mode];
  if (nice) return nice;
  // Sentence case, not Title Case: "Claude split" reads as one view's name,
  // "Claude Split" reads as a product.
  const words = mode.split(/[_-]+/).filter(Boolean);
  if (words.length === 0) return mode;
  return [words[0].charAt(0).toUpperCase() + words[0].slice(1), ...words.slice(1)].join(" ");
}
