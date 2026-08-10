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
// the URL or saved viewstate). The one target with NO menu and no preview is
// the self target (nothing selected — the folder already open on the left):
// see its branch below. Wire-up state (pane visibility, width, the divider
// drag) stays in Listing — this component only owns what the pane shows for a
// given selection.
import { useEffect, useState } from "react";
import { listDir, resolveConditions, statPath } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";
import { navigate, replaceSearch } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { isModeVisible } from "@platform/lib/mode-visibility";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES, templateModeIcon } from "@apps/explorer/ModeSwitcher";
import { ModeMenu } from "@apps/explorer/BarMenu";
import { useAppButton } from "@apps/explorer/lib/app-button";
import { withNoFocus } from "@apps/explorer/listing/frame-focus";
import { usePaneFocusGuard } from "@apps/explorer/listing/usePaneFocusGuard";
import Listing from "@apps/explorer/Listing";
import {
  PANE_APP_MODE,
  activePaneMode,
  paneModeList,
  paneOpenAction,
} from "@apps/explorer/listing/pane-modes";

// The selected row, as the pane needs it. Structurally a subset of Listing's
// RowCtx, so the lead row can be passed straight through.
export interface PaneTarget {
  path: string;
  name: string;
  isDir: boolean;
  // True when the target is the listing's OWN folder (nothing selected). It
  // has no preview of its own at all: name, the folder's primary action, and
  // the hint that says what to do. See the self branch below.
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
}: {
  // The lead row when exactly one row is selected, else null.
  row: PaneTarget | null;
  // Total selected rows, for the multi-selection placeholder.
  selCount: number;
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
  // The self target renders without asking the server anything (no modes, no
  // app probe — see its branch below), so neither fetch is started for it.
  const self = !!row?.self;
  useEffect(() => {
    if (!path || self) return;
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
  }, [path, self]);

  // Folder lone-app probe, for the `_app` mode. A truncated listing is only a
  // partial page (server cap), so a lone HTML match in it doesn't prove it is
  // the folder's ONLY one — offer no `_app` mode then (mirrors Listing's
  // onSingleApp guard). Errors also just drop the mode; the embedded Listing
  // surfaces the folder's real error itself.
  useEffect(() => {
    if (!path || !isDir || self) return;
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
  }, [path, isDir, self]);

  // The "Open as app" / "Add as app" button for a previewed FOLDER that holds a
  // lone top-level page — label, click and destination decided in one shared
  // place (lib/app-button), so this pane and the title bar's own button can
  // never mean different things. Called before the early returns below, as a
  // hook must be; a null folder switches the whole thing off and makes no
  // request, which covers files, the self target and a multi-selection alike.
  const appBtn = useAppButton(isDir && !self ? (path as string) : null, app?.path ?? null);

  // Keep the keyboard on the listing when this preview mounts (the pane's focus
  // contract — listing/frame-focus.ts). Also a hook, so also before the early
  // returns; the branches it guards are the ones that render a frame.
  const { rootRef, guardProps } = usePaneFocusGuard<HTMLDivElement>();

  // The pane's chrome strip, and EVERY state gets one — a loading skeleton, an
  // error, the metadata card and the multi-selection placeholder alike. It
  // keeps the strip's height agreeing with the search row beside it in every
  // state rather than most of them (see .pane-header).
  //
  // It used to open with a COLLAPSE button, sitting on the seam it sent the
  // pane back to. That went with the toggle: the split is now decided by the
  // container's width (listing/pane.ts), so a closed pane would have had no
  // way back short of resizing the window.
  //
  // `extra` is what the settled preview adds after the name, at the strip's
  // far end: the mode menu and the open-full-screen button.
  const strip = (extra?: React.ReactNode) => (
    <div className="pane-header">
      {row && (
        <span className="pane-header-icon">{iconForEntry(row.name, row.isDir)}</span>
      )}
      <span className="pane-header-name" title={row?.name}>
        {row?.name ?? ""}
      </span>
      {extra}
    </div>
  );

  // Placeholders need no fetch: nothing selected, or a multi-selection. No
  // subject, so the strip carries nothing but an empty name.
  if (!row) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-center">
          <div className="pane-hint">
            {selCount > 1 ? `${selCount} items selected` : "Select a file to preview."}
          </div>
        </div>
      </div>
    );
  }

  // The SELF target — nothing selected, so the pane's subject is the folder
  // already open on the left. It has no preview: just the folder's name and
  // the hint that says what to do about it. No actions either — the folder's
  // own primary ("Open as app") used to be handed down into this header, but a
  // folder that HAS an app is not empty, so FS-16's auto-select means this
  // row is barely ever on screen; the button belongs in the title bar, which
  // is where it now stays whether the pane is open or not (Preview.tsx).
  //
  // NO MODE MENU. The folder's peers under the `/` key are heavyweight opt-ins
  // (the chat, git, versions), so the picker's only job here was to offer a
  // chat on the folder from a header that otherwise said "select something" —
  // a "Choose view" chip pointing at a view nobody came for. The folder's own
  // modes are still one click from the LEFT half (Preview's chip), and every
  // entry in it previews on selection; and since opening a folder now
  // auto-selects its first entry (FS-16) and a background click no longer
  // deselects (FS-15), this state is reached essentially only by an empty
  // folder or by a deliberate Escape. With no picker there is no mode,
  // which is why this branch renders before any of the mode machinery — and
  // why the stat/app-probe fetches above skip the self target entirely.
  if (row.self) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-center">
          <div className="pane-hint">Select a file to preview.</div>
        </div>
      </div>
    );
  }

  if (info.status === "loading") {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-skel" />
      </div>
    );
  }
  if (info.status === "error") {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-center">
          <div className="status-message error">{info.message}</div>
        </div>
      </div>
    );
  }

  // --- the pane's mode list ---------------------------------------------------

  // Mode order = pane priority, decided in listing/pane-modes.ts — which also
  // documents why a lone app leads over `_listing`, and why `app` and `_app`
  // are never both offered. This component only turns those mode names into
  // menu entries with icons.
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
  // may still enter the list). This holds even with a `_panelMode` override
  // seeded from the URL: the override's mode may itself still be absent from
  // the interim list (a gated entry), so rendering before the list settles
  // could show the default and then jump. Both signals resolve exactly once per mount (the component is
  // keyed on the previewed path), and a user's switcher click can only happen
  // after they resolve, so this never re-shows the skeleton post-settle.
  const gatesPending =
    info.conditions === null &&
    info.templates.some((e) => e.conditional && e.mode !== "_listing");
  if (gatesPending || (row.isDir && app === undefined)) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-skel" />
      </div>
    );
  }
  // Default = the first mode in pane priority order; the menu override wins
  // while it's still offered. null = this row offers nothing at all, which
  // falls through to the metadata card at the bottom.
  const activeMode = activePaneMode(modeNames, modeOverride);
  const activeEntry = embeddable.find((e) => e.mode === activeMode) ?? null;

  // What the strip's far end offers for this row — decided in
  // listing/pane-modes (paneOpenAction), which documents why a plain folder
  // gets nothing: expanding one means opening its listing, and its listing is
  // what the left half of this very split already is.
  const open = paneOpenAction(row, activeMode);
  const openTarget = open.kind === "none" ? null : open.target;
  const goToTarget = () => {
    if (!openTarget) return;
    navigate(openTarget.path, { isDir: openTarget.isDir, mode: openTarget.mode });
  };

  // The settled header: the shared strip, with the mode control and (for a row
  // that has one) its open control after the name, at the far end of the strip.
  // Every OTHER state renders the bare strip instead — same chrome, nothing to
  // switch or expand yet. (The self target has its own, picker-less one and
  // returns above.)
  const header = strip(
    <>
      {/* The same mode control the title bar and the pane bars carry. It used
          to be four naked squares here, indistinguishable from the one-shot
          glyphs beside them. */}
      <ModeMenu entries={modes} active={activeMode ?? ""} onSelect={selectMode} />
      {/* Expand the previewed FILE full-screen — as a quiet icon, not the
          bordered "Open" primary it used to be. Nothing in this strip is a
          primary: the row's double-click and Enter already open it, so a
          bordered word was the loudest thing in the pane's header for the one
          action the user least needs pointed out. Two arrows to opposite
          corners is the expand/full-screen glyph, which is also the truer
          description — the preview is already open, this makes it the whole
          view. Plain .bar-ctl-icon metrics, like every other glyph-only control
          in these bars — it had a rule of its own for one release that only
          restated them.

          It opens in the mode the pane is SHOWING (paneOpenTarget) — "make this
          the whole view" cannot be the one action that discards the template
          the user picked. The row's own double-click and Enter stay a plain
          open: those are "open this thing", not "open what I am looking at". */}
      {open.kind === "expand" && (
        <button
          type="button"
          className="bar-ctl bar-ctl-icon"
          title="Open"
          aria-label="Open"
          onClick={goToTarget}
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
      {/* A folder that IS an app gets the folder's real primary in that slot
          instead, and this one is LABELLED: the expand glyph reads as "bigger",
          which is not what happens — the folder's page opens, and no icon says
          that. It is literally the title bar's own button for the open folder
          (lib/app-button), because it is the same action one level down — down
          to offering "Add as app" for a folder the registry doesn't know yet,
          which the pane's older, listing-only version of this button could not
          see and so could not offer. */}
      {appBtn && (
        <button type="button" className="bar-ctl" title={appBtn.label} onClick={appBtn.onClick}>
          {appBtn.label}
        </button>
      )}
    </>
  );

  // The /render embed URL for a chosen template entry. "_render" renders the
  // file itself (PT-12); a template mode renders the template against _file.
  //
  // Every one of them carries `_nofocus=1`: a preview in the pane must not take
  // the keyboard off the listing (listing/frame-focus.ts). It rides on the URL
  // rather than being passed some other way for the same reason `_file` does —
  // the page is a document, and its URL is the only thing it is handed.
  const srcFor = (t: TemplateEntry): string => {
    if (t.mode === "_render") return withNoFocus(`/render?path=${encodeURIComponent(row.path)}`);
    const remote = info.remote ? "&_remote=1" : "";
    return withNoFocus(
      `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(row.path)}${remote}`
    );
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
          src={withNoFocus(`/render?path=${encodeURIComponent(app.path)}`)}
          title={app.name}
        />
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
    // No embeddable template for a file: the bare strip (no mode to switch —
    // this row offers none) over a metadata card (icon, name, size, Open).
    body = (
      <>
        {strip()}
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
      </>
    );
  }

  // The only branch that renders a frame, and so the only one the focus guard
  // has anything to watch.
  return (
    <div className="listing-pane" ref={rootRef} {...guardProps}>
      {body}
    </div>
  );
}
