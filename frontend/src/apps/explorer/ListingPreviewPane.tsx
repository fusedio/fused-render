// Right-hand preview pane for the directory listing (views/Listing.tsx) —
// selection-driven: previews the listing's lead row when exactly one row is
// selected, and shows a neutral placeholder otherwise. Reuses the app's own
// template pipeline for FILES AND FOLDERS alike: the selection is stat'ed to
// discover its template modes and the default embeds as the normal /render
// iframe. A folder's `_listing` mode mounts the real shell Listing component
// (embedded — no URL writes, no nested pane, no global keyboard); a folder
// with a lone top-level HTML "app" additionally offers the pane-only `_app`
// mode that renders that app in place. The header carries a mode menu so the
// previewed mode can be switched (pane-local, transient — it never touches
// the URL or saved viewstate). Wire-up state (pane visibility, width, the
// divider drag) stays in Listing — this component only owns what the pane
// shows for a given selection.
import { useEffect, useState } from "react";
import { listDir, resolveConditions, statPath } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";
import { navigate, replaceSearch } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { isModeVisible } from "@platform/lib/mode-visibility";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES, templateModeIcon } from "@apps/explorer/ModeSwitcher";
import { ModeMenu } from "@apps/explorer/BarMenu";
import Listing from "@apps/explorer/Listing";
import { PANE_APP_MODE, activePaneMode, paneModeList } from "@apps/explorer/listing/pane-modes";

// The selected row, as the pane needs it. Structurally a subset of Listing's
// RowCtx, so the lead row can be passed straight through.
export interface PaneTarget {
  path: string;
  name: string;
  isDir: boolean;
  // True when the target is the listing's OWN folder (nothing selected): the
  // same mode logic runs, except `_listing` is never offered (that listing is
  // already on the left side of the split) and the pane lands on NO mode at
  // all — a neutral hint, with every offered mode one click away in the
  // switcher — instead of the folder's first opt-in mode. See
  // listing/pane-modes.ts.
  self?: boolean;
}

// The pane-only sentinel, re-exported locally for readability (defined with
// the ordering rules in listing/pane-modes.ts).
const APP_MODE = PANE_APP_MODE;

// Monochrome switcher icon for the `_app` mode — currentColor like the other
// mode icons, so it takes the switcher's muted/active tinting instead of the
// colored file icon (which read as an odd yellow button in the strip).
const APP_MODE_ICON = (
  <svg
    viewBox="0 0 24 24"
    width="16"
    height="16"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <path d="M10 8.5l6 3.5-6 3.5z" />
  </svg>
);

// The stat'ed template picture for the selected row. `conditions` is null
// while gated entries are still resolving (CT-12 deferred verdicts).
interface PaneInfo {
  templates: TemplateEntry[];
  conditions: Record<string, boolean> | null;
  size: number | null;
  remote: boolean;
}

type InfoState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | ({ status: "ok" } & PaneInfo);

// Lone-app detection for a folder: null while loading or when the folder has
// no single unambiguous app (a truncated listing never proves uniqueness).
interface AppTarget {
  name: string;
  path: string;
}

// One entry of the pane's mode menu (template modes + pane sentinels) — the
// shape BarMenu's shared ModeMenu takes.
interface PaneMode {
  mode: string;
  icon: React.ReactNode;
}

export default function ListingPreviewPane({
  row,
  selCount,
  selfPrimary,
}: {
  // The lead row when exactly one row is selected, else null.
  row: PaneTarget | null;
  // Total selected rows, for the multi-selection placeholder.
  selCount: number;
  // The host folder's own primary action, for the SELF row's primary slot
  // (see the header below). Built by the host so its state lives in one place.
  selfPrimary?: React.ReactNode;
}) {
  const [info, setInfo] = useState<InfoState>({ status: "loading" });
  // Lone-app probe result for a folder: undefined = still loading, null =
  // no unambiguous app. Only drives the `_app` menu entry, never a default.
  const [app, setApp] = useState<AppTarget | null | undefined>(undefined);
  // Mode override from the mode menu, synced to the URL as `_panelMode` so
  // the chosen pane mode SURVIVES selection switches: the component is keyed
  // on the previewed path (Listing) and remounts per selection, but each
  // mount re-seeds from the URL. A selection that doesn't offer the mode
  // falls back to its default (activeMode below) while the param stays put —
  // the next selection that does offer it picks it up again.
  const [modeOverride, setModeOverride] = useState<string | null>(
    () => new URLSearchParams(location.search).get("_panelMode")
  );
  const selectMode = (m: string) => {
    setModeOverride(m);
    const params = new URLSearchParams(location.search);
    params.set("_panelMode", m);
    replaceSearch(location.pathname + "?" + params.toString());
  };

  // Stat the selection — files and folders alike carry a template mode list
  // (folders at minimum the `_listing` sentinel). The cleanup flag is the
  // superseded-click guard: a stale async result for a row that's no longer
  // selected must not render.
  const path = row?.path;
  const isDir = row?.isDir;
  useEffect(() => {
    if (!path) return;
    let alive = true;
    setInfo({ status: "loading" });
    statPath(path).then(
      (st) => {
        if (!alive) return;
        // Same defensive filter as Preview (SPEC PT-12): an entry with
        // path===null whose mode isn't a recognized sentinel would build a
        // `path=null` render URL, so drop it before choosing.
        const templates = st.templates.filter(
          (e) => e.path !== null || KNOWN_SENTINEL_MODES.has(e.mode)
        );
        const base: InfoState = {
          status: "ok",
          templates,
          conditions: templates.some((e) => e.conditional) ? null : {},
          size: st.size,
          remote: !!st.remote,
        };
        setInfo(base);
        if (base.conditions !== null) return;
        resolveConditions(path).then(
          (r) => alive && setInfo({ ...base, conditions: r.conditions }),
          // No verdicts; lib/mode-visibility keeps verdict-less gated entries
          // visible so a failed probe can't empty this pane's mode menu.
          () => alive && setInfo({ ...base, conditions: {} })
        );
      },
      (err: Error) => alive && setInfo({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [path]);

  // Folder lone-app probe, for the `_app` mode. A truncated listing is only a
  // partial page (server cap), so a lone HTML match in it doesn't prove it is
  // the folder's ONLY one — offer no `_app` mode then (mirrors Listing's
  // onSingleApp guard). Errors also just drop the mode; the embedded Listing
  // surfaces the folder's real error itself.
  useEffect(() => {
    if (!path || !isDir) return;
    let alive = true;
    setApp(undefined);
    listDir(path).then(
      (data) => {
        if (!alive) return;
        const dir = (data.path || path).replace(/\/+$/, "");
        const apps = data.entries.filter((e) => isAppEntry(e.name, e.is_dir));
        setApp(
          !data.truncated && apps.length === 1
            ? { name: apps[0].name, path: dir + "/" + apps[0].name }
            : null
        );
      },
      () => alive && setApp(null)
    );
    return () => {
      alive = false;
    };
  }, [path, isDir]);

  // Placeholders need no fetch: nothing selected, or a multi-selection.
  if (!row) {
    return (
      <div className="listing-pane">
        <div className="pane-center">
          <div className="pane-hint">
            {selCount > 1 ? `${selCount} items selected` : "Select a file to preview."}
          </div>
        </div>
      </div>
    );
  }

  if (info.status === "loading") {
    return (
      <div className="listing-pane">
        <div className="pane-skel" />
      </div>
    );
  }
  if (info.status === "error") {
    return (
      <div className="listing-pane">
        <div className="pane-center">
          <div className="status-message error">{info.message}</div>
        </div>
      </div>
    );
  }

  // --- the pane's mode list ---------------------------------------------------

  // Mode order = pane priority, decided in listing/pane-modes.ts — which also
  // documents why a SELF target lands on no mode at all, why a lone app leads
  // over `_listing`, and why `app` and `_app` are never both offered.
  // This component only turns those mode names into menu entries with icons.
  //
  // Gate policy is the shared one (lib/mode-visibility), on every mode surface
  // alike: an entry hides only on an EXPLICIT denial. A denied override is not
  // pinned into the list — the active mode falls back to the pane's default
  // (`activePaneMode` guards it), matching Preview.
  const allowed = (e: TemplateEntry) => isModeVisible(e, info.conditions);
  const embeddable = info.templates.filter((e) => e.mode !== "_listing" && allowed(e));

  const modeNames = paneModeList({
    templates: info.templates,
    conditions: info.conditions,
    isDir: !!row.isDir,
    self: !!row.self,
    hasApp: !!app,
  });
  const byMode = new Map(info.templates.map((e) => [e.mode, e]));
  const modes: PaneMode[] = modeNames.map((m) => ({
    mode: m,
    icon: m === APP_MODE ? APP_MODE_ICON : templateModeIcon(byMode.get(m) as TemplateEntry),
  }));

  // While the mode list is still undecided, hold the skeleton — the pane must
  // never settle on an interim mode and then jump. Undecided means: the
  // folder's app probe is in flight (`_app` would lead the list), or any
  // gated template's verdict is unresolved (a higher-ranked conditional mode
  // may still enter the list — and for a self target, an all-conditional
  // list must not flash the "no preview" hint while a gate may yet allow).
  // This holds even with a `_panelMode` override seeded from the URL: the
  // override's mode may itself still be absent from the interim list (a gated
  // entry), so rendering before the list settles could show the default and
  // then jump. Both signals resolve exactly once per mount (the component is
  // keyed on the previewed path), and a user's switcher click can only happen
  // after they resolve, so this never re-shows the skeleton post-settle.
  const gatesPending =
    info.conditions === null &&
    info.templates.some((e) => e.conditional && e.mode !== "_listing");
  if (gatesPending || (row.isDir && app === undefined)) {
    return (
      <div className="listing-pane">
        <div className="pane-skel" />
      </div>
    );
  }
  // A self target with nothing offerable at all (no app, every template
  // gate-denied) gets the bare hint — no header, because there is no mode to
  // switch to. With modes to offer it falls through instead, so the header (and
  // its switcher) stays on screen beside the hint.
  if (row.self && modeNames.length === 0) {
    return (
      <div className="listing-pane">
        <div className="pane-center">
          <div className="pane-hint">Select a file to preview.</div>
        </div>
      </div>
    );
  }
  // Default = the first mode in pane priority order, or NO mode for a self
  // target without an app of its own; the menu override wins while it's still
  // offered.
  const activeMode = activePaneMode(modeNames, modeOverride, {
    self: !!row.self,
    hasApp: !!app,
  });
  const activeEntry = embeddable.find((e) => e.mode === activeMode) ?? null;

  // Header: name + mode menu + Open. Shown for every mode except the file
  // metadata card, which carries its own big-icon layout instead.
  const header = (
    <div className="pane-header">
      <span className="pane-header-icon">{iconForEntry(row.name, row.isDir)}</span>
      <span className="pane-header-name" title={row.name}>
        {row.name}
      </span>
      {/* The same mode control the title bar and the pane bars carry. It used
          to be four naked squares here, indistinguishable from the one-shot
          glyphs beside them. */}
      <ModeMenu entries={modes} active={activeMode ?? ""} onSelect={selectMode} />
      {/* Open the previewed row full-screen — as a quiet icon, not the bordered
          "Open" primary it used to be. Nothing in this strip is a primary: the
          row's double-click and Enter already open it, so a bordered word was
          the loudest thing in the pane's header for the one action the user
          least needs pointed out. Two arrows to opposite corners is the
          expand/full-screen glyph, which is also the truer description — the
          preview is already open, this makes it the whole view.
          Self target: "Open" would navigate to the folder already open, so the
          slot goes to the folder's own primary instead — today the host's
          "Open as app" (`selfPrimary`), which used to sit in the title bar
          competing with the mode control and the layout zone. A folder with no
          single app passes nothing and the slot stays empty. */}
      {row.self ? selfPrimary : (
        <button
          type="button"
          className="bar-ctl pane-header-btn"
          title="Open"
          aria-label="Open"
          onClick={() => navigate(row.path, { isDir: row.isDir })}
        >
          <svg
            viewBox="0 0 24 24"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M15 3h6v6" />
            <path d="M21 3l-7 7" />
            <path d="M9 21H3v-6" />
            <path d="M3 21l7-7" />
          </svg>
        </button>
      )}
    </div>
  );

  // The /render embed URL for a chosen template entry. "_render" renders the
  // file itself (PT-12); a template mode renders the template against _file.
  const srcFor = (t: TemplateEntry): string => {
    if (t.mode === "_render") return `/render?path=${encodeURIComponent(row.path)}`;
    const remote = info.remote ? "&_remote=1" : "";
    return `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(row.path)}${remote}`;
  };

  let body: React.ReactNode;
  if (activeEntry) {
    // Keyed on mode so switching modes replaces the iframe outright — never
    // reuse a live frame across different templates.
    body = (
      <>
        {header}
        <iframe
          key={activeEntry.mode}
          className="pane-frame"
          src={srcFor(activeEntry)}
          title={row.name}
        />
      </>
    );
  } else if (activeMode === APP_MODE && app) {
    body = (
      <>
        {header}
        <iframe
          key={APP_MODE}
          className="pane-frame"
          src={`/render?path=${encodeURIComponent(app.path)}`}
          title={app.name}
        />
      </>
    );
  } else if (activeMode === null && row.self) {
    // "Nothing previewed" — the self target's default state, not a mode: the
    // header (so every offered mode stays one click away in the switcher, none
    // of them marked active) above the same hint the nothing-to-show path
    // shows.
    //
    // `row.self` is load-bearing, not defensive. `activePaneMode` returns null
    // for TWO unrelated reasons: this state, and `modes[0] ?? null` on an empty
    // list — which a SELECTED row reaches when its file maps to nothing or every
    // template is gate-denied. That row has a subject and must keep falling
    // through to the metadata card below; only the self target means "no subject
    // chosen yet". The predecessor sentinel (`NONE_MODE`) could not be produced
    // by an empty list, so it never had to say so.
    body = (
      <>
        {header}
        <div className="pane-center">
          <div className="pane-hint">Select a file to preview.</div>
        </div>
      </>
    );
  } else if (activeMode === "_listing" && row.isDir) {
    // The real shell Listing, embedded: full sorting/search/selection UI, but
    // URL-silent, paneless and without document-level keyboard (see Listing's
    // `embedded` prop).
    body = (
      <>
        {header}
        <Listing key={row.path} fsPath={row.path} embedded />
      </>
    );
  } else {
    // No embeddable template for a file: metadata card (icon, name, size, Open).
    body = (
      <div className="pane-center">
        <div className="pane-big-icon">{iconForEntry(row.name, false)}</div>
        <div className="pane-title">{row.name}</div>
        <div className="pane-hint">
          No preview for this file type{info.size !== null ? ` — ${formatSize(info.size)}` : ""}.
        </div>
        <button
          type="button"
          className="pane-open-btn"
          onClick={() => navigate(row.path, { isDir: false })}
        >
          Open
        </button>
      </div>
    );
  }

  return <div className="listing-pane">{body}</div>;
}
