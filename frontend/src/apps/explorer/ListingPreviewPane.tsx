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
//            mode that is, is listing/pane-modes.ts's call). **A SELECTED FOLDER
//            IS PREVIEWED AS A FOLDER** (D280): its own modes, led by `claude`
//            (the chat about it) with the `_listing` sentinel behind — the latter
//            mounting the real shell Listing component (embedded — no URL writes,
//            no nested pane, no global keyboard). It used to resolve a folder
//            holding a top-level `.html` to that PAGE and preview it as a file
//            (D269's retarget), which meant merely SELECTING a row ran the
//            folder's app; that is gone, and nothing here previews a folder as
//            anything but the folder.
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
import { withNoFocus } from "@platform/lib/frame-focus";
import { usePaneFocusGuard } from "@apps/explorer/listing/usePaneFocusGuard";
import { dropStaleChatParams } from "@apps/explorer/listing/chat-params";
import { pathFromSelParam } from "@apps/explorer/listing/selection";
import Listing from "@apps/explorer/Listing";
import {
  activePaneMode,
  paneChatOnly,
  paneModeList,
} from "@apps/explorer/listing/pane-modes";
import {
  paneSideMenu,
  paneSideTarget,
  type PaneSide,
  type PaneSideChoice,
  type PaneSideEntries,
} from "@apps/explorer/listing/pane-side";

// The query the chat template reads to give up its own preview pane (see
// paneChatOnly, which decides WHEN it is sent). Spelled once: both places that
// send it build a different URL around it.
const CHAT_ONLY_PARAM = "&chat_only=1";

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
  undecided = false,
  folder,
  side,
  sideEntries,
  appEntry,
  onOpenApp,
  onSelectSide,
  onClose,
}: {
  // The lead row when exactly one row is selected, else null.
  row: PaneTarget | null;
  // Total selected rows, for the multi-selection placeholder.
  selCount: number;
  // The pane has NO MODE YET — a selected folder row whose companion probes are
  // still out (pane-side's paneSideList answers an empty list, and `side` is then
  // only a placeholder for the pill's label). Renders the skeleton: resolving
  // anything here would put the pill on "Preview" while the row's own `claude`
  // default rendered a chat under it, and would respawn `agent.py` on the remount
  // when the probe landed.
  undecided?: boolean;
  // The OPEN folder — the listing on the other side of the divider. `Git`'s
  // subject, and `Claude`'s when the selection names no single row.
  folder: string;
  // Which of the three the pane is showing. Already resolved against what this
  // folder offers (pane-side's activePaneSide, in Listing), so it is always a
  // mode `sideEntries` can actually back.
  side: PaneSide;
  // The FOLDER's `claude` / `git` / `mcp` template entries, or null where the folder does
  // not offer the mode (lib/dir-mode). Resolved by Listing, which does not remount
  // per selection — see the module comment.
  sideEntries: PaneSideEntries;
  onSelectSide: (side: PaneSideChoice) => void;
  // The pane SUBJECT's entry page (`index.html`), or null when it has none — the
  // "Open app" button in the strip. Resolved by Listing, which already owns the
  // subject and its settle: this component is one of the things keyed on that
  // subject, so it must not go asking the question a second time and risk a
  // different answer from the one the pane is showing.
  appEntry: string | null;
  // Opens it. The caller's own `navigate`, for the reason the whole button lives in
  // Listing: an EMBEDDED pane may not move the host view, and the way that stays
  // true is that this component never navigates.
  onOpenApp: () => void;
  // Shuts the pane (`_side=off`). The listing's search row grows the reopening
  // half of the affordance while the pane is down — SideChrome writes the split
  // between the two down.
  onClose: () => void;
}) {
  const [info, setInfo] = useState<InfoState>({ status: "loading" });
  // The self target renders without asking the server anything (no modes — see
  // its branch below), so no fetch is started for it.
  const self = !!row?.self;
  // Only `Preview` is about the row's own templates. Claude and Git are handed
  // their entry by the caller and aimed by `_file`, so in those two modes the
  // stat below would be work for an answer nothing reads — including on every
  // selection change, since Claude stays mounted across one.
  //
  // Since D285 `preview` is only ever the neither-companion FALLBACK, so this stat —
  // and the row modes, metadata card and expand affordance it feeds — is reached only
  // in a folder where both gates refuse (a mount). That is still a real, reachable
  // state and the pane genuinely previews a file row's own template there, so none of
  // it is dead; it is just rare. Whether it should stay is not this change's question.
  const previewing = side === "preview";

  // --- A SELECTED FOLDER IS PREVIEWED AS A FOLDER (D280, deleting D269's pane
  // half) ---------------------------------------------------------------------
  //
  // There is no retarget here, and there must not be one. D269 resolved a folder
  // holding a top-level `.html` to that PAGE and previewed it as an ordinary
  // file, on the rule that such a folder IS its page. In this pane that turned a
  // SELECTION into an execution: arrowing onto a row mounted `/render` for the
  // folder's app, ran its template's Python, and put a live UI with working
  // buttons in the sidebar for a folder the user had done nothing but highlight.
  // The owner's words were "we don't want rendering".
  //
  // So the pane stats the SELECTED ROW itself, folder or file, and a folder
  // resolves to the FOLDER's mode list — `claude` first, then the `_listing`
  // sentinel (registry.json's universal `/` key, D280). Nothing about the page it
  // may contain enters into it; the page is one row of the embedded listing, one
  // click away, exactly as it is in the listing on the left.
  //
  // (D269's OTHER half is untouched: an app CARD on the hub still opens the entry
  // page, and the server keeps its own entry rule — `app_listing.app_entry`,
  // `templates/shared/app_entry.py` — for the surfaces that ask "which page is
  // this folder". This pane simply stops asking — and the frontend copy of the
  // rule, `apps/explorer/lib/app-entry.ts`, is DELETED, since this pane was its
  // only caller. Re-deriving it here is the change to refuse.)

  // Stat the selected row to discover its modes. Files and folders alike carry a
  // template mode list (folders at minimum the `_listing` sentinel). The cleanup
  // flag is the superseded-click guard: a stale async result for a row that's no
  // longer selected must not render.
  const path = row?.path;
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
  // contract — platform/lib/frame-focus.ts). Also a hook, so also before the early
  // returns; the branches it guards are the ones that render a frame.
  const { rootRef, guardProps } = usePaneFocusGuard<HTMLDivElement>();

  // Retargeting the chat takes its params off the URL (listing/chat-params.ts has
  // the why). The condition mirrors the Claude branch below exactly, so the target
  // is only claimed on renders where the chat iframe is actually up — a skeleton
  // frame or a flip through Git never reads as a retarget. A hook, so above the
  // early returns like the two before it. `urlNamed` — this target is the one the
  // url's own `?sel=` (or its absence) points at, i.e. a seeded selection playing
  // out rather than the user clicking away from a chat — is what keeps a deep
  // link's params alive through the mount-time folder→row hop (chat-params.ts).
  const chatTarget =
    !undecided && side === "claude" && sideEntries.claude && sideEntries.claude.path !== null
      ? paneSideTarget("claude", folder, row && !row.self ? row.path : null)
      : null;
  // `?sel=` decodes against the SAME base the rows are built on — Listing's
  // fsPath with its trailing slash stripped (useListingSelection) — or the two
  // spellings diverge exactly at the roots: "/" would decode `?sel=x` to "//x"
  // while the row is "/x", and "C:/" likewise, so a seeded hop at a root would
  // never count as url-named and a deep link's params would be stripped there.
  const urlNamedTarget =
    chatTarget !== null &&
    chatTarget ===
      (pathFromSelParam(
        folder.replace(/\/$/, ""),
        new URLSearchParams(location.search).get("sel")
      ) ?? folder);
  useEffect(() => {
    dropStaleChatParams(chatTarget, urlNamedTarget);
  }, [chatTarget, urlNamedTarget]);

  // --- the pane's modes (listing/pane-side.ts) --------------------------------
  // EVERY MODE THE PANE MAY BE ON, and never fewer: an unofferable COMPANION is
  // drawn disabled with its reason, or spinning while its probe is out
  // (paneSideMenu), rather than dropped — a menu that shrinks to one row hides
  // itself, which once left a mount-backed folder's pane header a lone chevron.
  //
  // **The list is no longer row-independent, and that is a real cost** (D281): a
  // selected FOLDER row has no `preview`, so the pill is three rows over a file and
  // two over a folder, and it changes shape as the user arrows from one onto the
  // other. It used to hold perfectly still — "nothing about the selected row enters
  // into it" was the claim here — and the reason it no longer can is that `preview`
  // for a folder meant either running that folder's app (D280) or a listing of a
  // folder beside the listing it was selected in. A control that is on offer,
  // selected, and wrong is worse than one that changes width.
  //
  // What still holds still is the COMPANIONS' own availability, which comes from
  // the FOLDER and not from the row — so walking from a repository into a folder
  // outside one does not make the Git row appear and disappear, it dims.
  const sideMenu = (
    <ModeMenu
      entries={paneSideMenu(sideEntries).map((e) => ({
        ...e,
        icon: paneSideIcon(e.mode, sideEntries),
      }))}
      active={side}
      onSelect={(m) => onSelectSide(m as PaneSideChoice)}
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
  //
  // "Open app" rides in here rather than in one branch, so it is present in every
  // pane state the way the chevron and the pill are — including the skeleton, where
  // the subject is known long before the pane has resolved what to show about it.
  // Ahead of the pill so the pill keeps the strip's right end, where the expand
  // button (`extra`) also lands.
  const strip = (extra?: React.ReactNode) => (
    <div className="pane-header">
      <SideCloseButton what={modeTitle(side)} onClick={onClose} />
      <div className="side-header-tail">
        {appEntry && (
          <button
            type="button"
            className="bar-ctl bar-ctl-strong"
            title={"Open " + appEntry.slice(appEntry.lastIndexOf("/") + 1)}
            onClick={onOpenApp}
          >
            Open app
          </button>
        )}
        {sideMenu}
        {extra}
      </div>
    </div>
  );

  // --- no mode yet -----------------------------------------------------------
  // BEFORE every branch that could render something, including the companions:
  // while this is true there is no answer to render, and each of the paths below
  // would invent one (the companions from a null entry, and failing that the row's
  // own default mode — a chat under a pill reading "Preview", which is the reported
  // bug). The pill itself is in `strip` here as everywhere, drawing its two
  // companions as spinners (paneSideMenu), so the header says "resolving" while the
  // body does.
  if (undecided) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-skel" />
      </div>
    );
  }

  // --- Claude, Git and MCP: the companions -----------------------------------
  // All of them render straight from the FOLDER's entry, with no question asked
  // about the selected row, so they come BEFORE every one of the row-driven
  // branches below (the loading skeleton, the placeholders, the self target). The
  // folder-bound two have to: their subject IS the folder, so a folder with
  // nothing selected still has a working tree and a manifest to show.
  //
  // `_file` is where they differ, and it is the whole of the difference — every
  // template is used exactly as it ships. paneSideTarget says which: the folder
  // for Git and MCP, the selected row for Claude (the folder again when the
  // selection names no single row, so the chat has something to be about).
  const sideEntry = side === "preview" ? null : sideEntries[side];
  if (sideEntry && sideEntry.path !== null) {
    const target = paneSideTarget(side, folder, row && !row.self ? row.path : null);
    // `chat_only=1` takes away the chat template's OWN left preview pane — the
    // rule and its two reasons are on paneChatOnly. `_remote` is deliberately
    // absent from all of them: Claude reads through the server either way, and the
    // git and mcp gates refuse a mount-backed directory outright.
    const chatOnly = paneChatOnly(side) ? CHAT_ONLY_PARAM : "";
    return (
      <div className="listing-pane" ref={rootRef} {...guardProps}>
        {strip()}
        <iframe
          className="pane-frame"
          src={withNoFocus(
            `/render?path=${encodeURIComponent(sideEntry.path)}` +
              `&_file=${encodeURIComponent(target)}${chatOnly}&_preview=1`
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

  // The SELF target — nothing selected, so the pane's subject is the folder already
  // open on the left. It has no preview: just the hint that says what to do about it.
  //
  // **THIS IS NOW A FALLBACK, not the no-selection state's answer** (D284). Nothing
  // selected is a FOLDER subject, so the pane lands on the chat about that folder
  // wherever a companion is offered, and those branches return above this one. What
  // reaches here is the case where NEITHER companion is (a mount-backed folder, both
  // gates refusing) — and there this hint is the pane's only possible content, which
  // is why the branch stays. *Until D284 it was the answer for every folder open,
  // which is what the owner reported: a "Select a file to preview." hint under a pill
  // reading "Preview", over a folder that is not previewable.*
  //
  // NO PER-ROW MODE LIST, which is why this branch renders before all the mode
  // machinery below and why the stat above skips the self target entirely:
  // there is no row to resolve templates for. The pane's OWN three-way
  // pill is in `strip` and so is present here like everywhere else — drawing its two
  // companions disabled with their reasons, which in this state is the whole
  // explanation of why the hint is what is left. The picker that was removed here
  // was the per-row one: its only job was to offer a chat on the folder from a
  // header that otherwise said "select something", i.e. a "Choose view" chip
  // pointing at a view nobody came for. Claude-on-the-folder is a named mode of the
  // pane now — and, since D284, the mode this state lands on whenever it can.
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

  // The row is still stat'ing: one skeleton, so the pane never paints an interim
  // answer and then swaps it.
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
  //
  // The subject is the SELECTED ROW, always. `view` used to be able to differ
  // from `row` — an app folder was retargeted to its entry page, so the mode
  // list, the embed URL, the expand target and the header title were all the
  // page's — and that indirection is gone with the retarget (D280). The name
  // stays because everything below reads it, and keeping it makes the one thing
  // it means explicit: what the pane is showing, which is now always what the
  // user selected.
  const view: PaneTarget = row;

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
    isDir: !!view.isDir,
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

  // The settled Preview's header is the bare shared strip (chevron, folder
  // action, the pane's three-way pill). The full-screen expand glyph that used
  // to sit at its far end is gone: the row's double-click and Enter already
  // open the file, so the strip offers nothing for it.
  const header = strip();

  // The /render embed URL for a chosen template entry. "_render" renders the
  // file itself (PT-12); a template mode renders the template against _file.
  //
  // Every one of them carries `_nofocus=1`: a preview in the pane must not take
  // the keyboard off the listing (platform/lib/frame-focus.ts). It rides on the URL
  // rather than being passed some other way for the same reason `_file` does —
  // the page is a document, and its URL is the only thing it is handed.
  //
  // A row mode can be `claude` — it is a FOLDER's default (D280) — and that one
  // needs its own pane taken away exactly as the companion above does, or the
  // chat template resolves the folder's entry page and renders the app inside
  // this column. paneChatOnly is the single rule both ask.
  const srcFor = (t: TemplateEntry): string => {
    // `_preview=1`: a listing's side pane is a preview, not an open — without
    // it the server would record recency (and register an external folder) for
    // every file the user merely selects (D301).
    if (t.mode === "_render")
      return withNoFocus(`/render?path=${encodeURIComponent(view.path)}&_preview=1`);
    const remote = info.remote ? "&_remote=1" : "";
    const chatOnly = paneChatOnly(t.mode) ? CHAT_ONLY_PARAM : "";
    return withNoFocus(
      `/render?path=${encodeURIComponent(t.path as string)}` +
        `&_file=${encodeURIComponent(view.path)}${remote}${chatOnly}`
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
          title={view.name}
        />
      </>
    );
  } else if (activeMode === "_listing" && view.isDir) {
    // The real shell Listing, embedded: full sorting/search/selection UI, but
    // URL-silent, paneless and without document-level keyboard (see Listing's
    // `embedded` prop).
    body = (
      <>
        {header}
        <Listing key={view.path} fsPath={view.path} embedded />
      </>
    );
  } else {
    // No embeddable template for a file: the bare strip (no mode to switch —
    // this row offers none) over a metadata card (icon, name, size, Open).
    body = (
      <>
        {strip()}
        <div className="pane-center">
          <div className="pane-big-icon">{iconForEntry(view.name, false)}</div>
          <div className="pane-title">{view.name}</div>
          <div className="pane-hint">
            No preview for this file type{info.size !== null ? ` — ${formatSize(info.size)}` : ""}.
          </div>
          <button
            type="button"
            className="pane-open-btn"
            onClick={() => navigate(view.path, { isDir: false })}
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
