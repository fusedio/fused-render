// Right-hand preview pane for the directory listing (views/Listing.tsx), and the
// folder's half of the companion split the file view grew (Preview.tsx /
// PreviewSidebar.tsx). It shows ONE OF THREE things — listing/pane-side.ts owns
// the list and the `_side` param that records which:
//
//   Preview  the selection-driven preview it has always been: the listing's lead
//            row when exactly one row is selected, a neutral placeholder
//            otherwise. Reuses the app's own template pipeline for FILES AND
//            FOLDERS alike — the selection is stat'ed to discover its template
//            modes and its DEFAULT embeds as the normal /render iframe (which
//            mode that is, is listing/pane-modes.ts's call). A folder's
//            `_listing` mode mounts the real shell Listing component (embedded —
//            no URL writes, no nested pane, no global keyboard).
//   Claude   the chat, chat-only, about the selected row (about the FOLDER when
//            the selection names no single row).
//   Git      the OPEN FOLDER's working tree. The one mode whose subject is not
//            the selection at all — see paneKey on why that matters.
//
// The two companions' template entries are the FOLDER's, resolved once by Listing
// (lib/dir-mode) and handed down: neither changes with the selection, and this
// component remounts on every selection change.
//
// The header is the file sidebar's header, to the button: the way out of the
// column at its left end, the mode pill at its right (SideChrome). Wire-up state
// — whether there is a pane at all, its width, the divider drag, and `_side`
// itself — stays in Listing; this component owns only what the pane shows.
import { useEffect, useState } from "react";
import { resolveConditions, statPath } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { modeTitle } from "@platform/lib/mode-name";
import { isModeVisible } from "@platform/lib/mode-visibility";
import { iconForEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import { ModeMenu } from "@apps/explorer/BarMenu";
import { SideCloseButton, paneSideIcon } from "@apps/explorer/SideChrome";
import { withNoFocus } from "@apps/explorer/listing/frame-focus";
import { usePaneFocusGuard } from "@apps/explorer/listing/usePaneFocusGuard";
import Listing from "@apps/explorer/Listing";
import {
  activePaneMode,
  paneModeList,
  paneOpenAction,
} from "@apps/explorer/listing/pane-modes";
import {
  paneSideList,
  paneSideTarget,
  type PaneSide,
  type PaneSideEntries,
} from "@apps/explorer/listing/pane-side";

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

export default function ListingPreviewPane({
  row,
  selCount,
  folder,
  side,
  sideEntries,
  onSelectSide,
  onClose,
}: {
  // The lead row when exactly one row is selected, else null.
  row: PaneTarget | null;
  // Total selected rows, for the multi-selection placeholder.
  selCount: number;
  // The OPEN folder — the listing on the other side of the divider. `Git`'s
  // subject, and `Claude`'s when the selection names no single row.
  folder: string;
  // Which of the three the pane is showing. Already resolved against what this
  // folder offers (pane-side's activePaneSide, in Listing), so it is always a
  // mode `sideEntries` can actually back.
  side: PaneSide;
  // The FOLDER's `claude` / `git` template entries, or null where the folder does
  // not offer the mode (lib/dir-mode). Resolved by Listing, which does not remount
  // per selection — see the module comment.
  sideEntries: PaneSideEntries;
  onSelectSide: (side: PaneSide) => void;
  // Shuts the pane (`_side=off`). The listing's search row grows the reopening
  // half of the affordance while the pane is down — SideChrome writes the split
  // between the two down.
  onClose: () => void;
}) {
  const [info, setInfo] = useState<InfoState>({ status: "loading" });
  // Stat the selection — files and folders alike carry a template mode list
  // (folders at minimum the `_listing` sentinel). The cleanup flag is the
  // superseded-click guard: a stale async result for a row that's no longer
  // selected must not render.
  const path = row?.path;
  // The self target renders without asking the server anything (no modes — see
  // its branch below), so no fetch is started for it.
  const self = !!row?.self;
  // Only `Preview` is about the row's own templates. Claude and Git are handed
  // their entry by the caller and aimed by `_file`, so in those two modes the
  // stat below would be work for an answer nothing reads — including on every
  // selection change, since Claude stays mounted across one.
  const previewing = side === "preview";
  useEffect(() => {
    if (!path || self || !previewing) return;
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
  }, [path, self, previewing]);

  // Keep the keyboard on the listing when this preview mounts (the pane's focus
  // contract — listing/frame-focus.ts). Also a hook, so also before the early
  // returns; the branches it guards are the ones that render a frame.
  const { rootRef, guardProps } = usePaneFocusGuard<HTMLDivElement>();

  // --- the pane's three modes (listing/pane-side.ts) --------------------------
  // Which are on offer follows entirely from what the FOLDER gave us: `preview`
  // always, the companions only where the folder has an entry for them. Nothing
  // about the selected row enters into it, which is what lets the pill hold still
  // as the user arrows down the list.
  const sides = paneSideList(sideEntries);
  const sideMenu = (
    <ModeMenu
      entries={sides.map((m) => ({ mode: m, icon: paneSideIcon(m, sideEntries) }))}
      active={side}
      onSelect={(m) => onSelectSide(m as PaneSide)}
    />
  );

  // The pane's chrome strip, and EVERY state gets one — a loading skeleton, an
  // error, the metadata card and the multi-selection placeholder alike. It is
  // the TOP BAR of the right-hand column now that the pane runs the full height
  // of the window, so it has to hold its height in every state or the seam it
  // shares with the crumb bar on the left breaks (see .pane-header).
  //
  // It opens with the way OUT of the column and it ends with the mode pill, which
  // is the file sidebar's header exactly (SideChrome, PreviewSidebar) — the two
  // columns are the same column over a folder and over a file, so they wear the
  // same bar.
  //
  // Both of those are in `strip` rather than in `extra`, i.e. in every state: a
  // control that vanishes while a row stats is a control the user cannot rely on,
  // and closing the pane is not something a loading preview should be able to
  // take away. The OPEN FOLDER's primary action used to be portaled in beside
  // them on the same reasoning — it belonged to the folder on the left, not to
  // whichever row the pane happens to be showing — but that action was "Open as
  // app" and a folder has no primary any more (D264).
  //
  // The chevron went missing for a while in between. It used to sit on the seam it
  // sent the pane back to, and went with the toggle when visibility became purely
  // a measurement of the container's width (listing/pane.ts) — a closed pane had
  // no way back short of resizing the window. It is back because the reopening
  // half is back with it, in the listing's search row.
  //
  // The strip also used to carry the previewed row's ICON AND NAME, and those are
  // still gone: the row is highlighted an inch to the left with its name in the
  // same eyeline, so restating it here put the loudest text in the strip on the
  // one fact the layout already made obvious.
  //
  // `extra` is what only the settled Preview puts in it — the open-full-screen
  // button — at the far end, after the pill.
  const strip = (extra?: React.ReactNode) => (
    <div className="pane-header">
      <SideCloseButton what={modeTitle(side)} onClick={onClose} />
      <div className="side-header-tail">
        {sideMenu}
        {extra}
      </div>
    </div>
  );

  // --- Claude and Git: the companions ----------------------------------------
  // Both render straight from the FOLDER's entry, with no question asked about
  // the selected row, so they come BEFORE every one of the row-driven branches
  // below (the loading skeleton, the placeholders, the self target). Git in
  // particular has to: its subject is the folder, so a folder with nothing
  // selected still has a working tree to show.
  //
  // `_file` is where the two differ, and it is the whole of the difference —
  // both templates are used exactly as they ship. paneSideTarget says which:
  // the folder for Git, the selected row for Claude (the folder again when the
  // selection names no single row, so the chat has something to be about).
  const sideEntry = side === "claude" ? sideEntries.claude : side === "git" ? sideEntries.git : null;
  if (sideEntry && sideEntry.path !== null) {
    const target = paneSideTarget(side, folder, row && !row.self ? row.path : null);
    // `chat_only=1` takes away the chat template's OWN left preview pane: in a
    // column this narrow its copy of the target would be a second, differently
    // run preview of the same thing (see Preview's sideSrcFor, and CHAT_ONLY in
    // templates/claude/template.html). `_remote` is deliberately absent from
    // both: Claude reads through the server either way, and the git gate refuses
    // a mount-backed directory outright.
    const chatOnly = side === "claude" ? "&chat_only=1" : "";
    return (
      <div className="listing-pane" ref={rootRef} {...guardProps}>
        {strip()}
        <iframe
          className="pane-frame"
          src={withNoFocus(
            `/render?path=${encodeURIComponent(sideEntry.path)}` +
              `&_file=${encodeURIComponent(target)}${chatOnly}`
          )}
          title={modeTitle(side)}
        />
      </div>
    );
  }

  // Placeholders need no fetch: nothing selected, or a multi-selection. No row to
  // preview, so the strip is the bare one — its chevron and its three-way pill,
  // which are about the PANE and the folder rather than about the missing row.
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
  // already open on the left. It has no preview: just the hint that says what to
  // do about it.
  //
  // NO PER-ROW MODE LIST, which is why this branch renders before all the mode
  // machinery below and why the stat above skips the self target entirely:
  // there is no row to resolve templates for. The pane's OWN three-way
  // pill is in `strip` and so is present here like everywhere else — and it is
  // exactly the control this state used to lack. The picker that was removed here
  // was the per-row one: its only job in this state was to offer a chat on the
  // folder from a header that otherwise said "select something", i.e. a "Choose
  // view" chip pointing at a view nobody came for. Claude-on-the-folder is now a
  // named mode of the pane rather than a mode of the absent row, so it is offered
  // plainly instead of hidden behind a picker with nothing else in it.
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

  // --- what `Preview` resolves to for this row --------------------------------

  // Mode order = pane priority, decided in listing/pane-modes.ts. This
  // component only turns that ranking into a rendered frame.
  //
  // Gate policy is the shared one (lib/mode-visibility), on every mode surface
  // alike: an entry hides only on an EXPLICIT denial.
  //
  // What is gone is the CHOICE among them. The pane's header used to carry a
  // switcher over this whole list, synced to `_panelMode` — one control for the
  // row's templates, in a bar that now carries the pane's own three
  // (listing/pane-side.ts explains the trade). So the list is read for its LEAD
  // only: `Preview` means "this row's default view", and `activePaneMode` is
  // called with no override.
  const allowed = (e: TemplateEntry) => isModeVisible(e, info.conditions);
  const embeddable = info.templates.filter((e) => e.mode !== "_listing" && allowed(e));

  const modeNames = paneModeList({
    templates: info.templates,
    conditions: info.conditions,
    isDir: !!row.isDir,
  });

  // While that list is still undecided, hold the skeleton — the pane must never
  // settle on an interim mode and then jump. Undecided means a gated template's
  // verdict is unresolved (a higher-ranked conditional mode may still enter the
  // list). It resolves exactly once per mount (the component is keyed on the
  // previewed path), so this never re-shows the skeleton post-settle.
  const gatesPending =
    info.conditions === null &&
    info.templates.some((e) => e.conditional && e.mode !== "_listing");
  if (gatesPending) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-skel" />
      </div>
    );
  }
  // The row's DEFAULT view — the first mode in pane priority order, with no
  // override to beat it (see above). null = this row offers nothing at all, which
  // falls through to the metadata card at the bottom.
  const activeMode = activePaneMode(modeNames, null);
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

  // The settled Preview's header: the shared strip (chevron, folder action, the
  // pane's three-way pill) plus, at its very end, the controls that only mean
  // something once a row's own view has resolved. Every OTHER state renders the
  // bare strip — same chrome, nothing to expand yet.
  const header = strip(
    <>
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
          the whole view" cannot be the one action that discards what is on
          screen. The row's own double-click and Enter stay a plain open: those
          are "open this thing", not "open what I am looking at".
          Only in `Preview`, since only there is the pane showing the row's own
          content. Claude and Git return well above this (and Git is not about
          the row at all), so neither reaches here — "make this the whole view"
          for a companion is a question about the FILE view's `_side`, and the way
          to ask it is to open the row and use the sidebar there. */}
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
