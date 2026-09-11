// Right-hand preview pane for the directory listing (views/Listing.tsx): the
// OPEN FOLDER's companions — `claude` (chat about the folder) and `git` (its
// working tree) — never the selection. (`mcp`, its MCP tools, is a dialog off the
// search row's kebab rather than a pane mode.)
// listing/pane-side.ts owns the list and the `_side` param that records which.
//
// **THE PANE NO LONGER FOLLOWS THE SELECTION AT ALL** (D460, superseding
// D280/D281/D284's whole arc). It used to be selection-driven: a selected row's
// own template (`Preview`), Claude's target following whichever row was
// selected, a selected folder previewed as an embedded listing. All of that
// read the listing's selection, and every one of the bugs those four decisions
// fixed one at a time (a folder's app running merely because it was
// highlighted, a stale companion following a row across a rename, a "Select a
// file to preview." hint under a pill reading something else) was some shape
// of "the pane's content changed for a reason the user did not click" — which
// a pane that shows the OPEN FOLDER's own companions cannot do at all, because
// there is nothing left for it to read off the selection. `folder` is the
// pane's only subject now, on every mode, always.
//
// The fallback (`side === "preview"`, `PANE_SIDE_FALLBACK`) is what the pane
// shows when NEITHER companion is offered (a mount-backed folder, where both
// gates refuse) — a plain folder-scoped hint, never a row's template: there is
// no row to resolve one for any more.
//
// The header is the file sidebar's header, to the button: the way out of the
// column at its left end, the mode pill at its right (SideChrome). Wire-up
// state — whether there is a pane at all, its width, the divider drag, and
// `_side` itself — stays in Listing; this component owns only what the pane
// shows.
import { modeTitle } from "@platform/lib/mode-name";
import { withNoFocus } from "@platform/lib/frame-focus";
import { ChatMount, listingPaneSrc } from "@apps/claude";
import { usePaneFocusGuard } from "@apps/explorer/listing/usePaneFocusGuard";
import { SideCloseButton, SideTabs, paneSideIcon } from "@apps/explorer/SideChrome";
import { paneChatOnly } from "@apps/explorer/listing/pane-modes";
import {
  paneSideMenu,
  type PaneSide,
  type PaneSideChoice,
  type PaneSideEntries,
} from "@apps/explorer/listing/pane-side";

// The query the chat template reads to give up its own preview pane (see
// paneChatOnly, which decides WHEN it is sent). Spelled once: both places that
// send it build a different URL around it.
const CHAT_ONLY_PARAM = "&chat_only=1";

export default function ListingPreviewPane({
  undecided = false,
  folder,
  side,
  sideEntries,
  onSelectSide,
  onClose,
  initialAsk,
}: {
  // The pane has NO MODE YET — the folder's companion probes are still out
  // (pane-side's paneSideList answers an empty list, and `side` is then only a
  // placeholder for the pill's label). Renders the skeleton: resolving
  // anything here would put the pill on "Preview" while the wrong companion
  // rendered under it, and would respawn `agent.py` on the remount when the
  // probe landed.
  undecided?: boolean;
  // The OPEN folder — every mode's subject now (D460).
  folder: string;
  // Which of the three the pane is showing. Already resolved against what this
  // folder offers (pane-side's activePaneSide, in Listing), so it is always a
  // mode `sideEntries` can actually back, or the neither-companion fallback.
  side: PaneSide;
  // The FOLDER's `claude` / `git` / `mcp` template entries, or null where the folder does
  // not offer the mode (lib/dir-mode). Resolved by Listing, which does not remount
  // per selection — see the module comment.
  sideEntries: PaneSideEntries;
  onSelectSide: (side: PaneSideChoice) => void;
  // The "Fix with AI" prompt, already pulled-and-cleared by Listing (which owns
  // `window._fusedClaudeAskTake`), and only when the NATIVE chat is what renders
  // — flag off, the template pulls it at its own boot and this is null.
  initialAsk?: string | null;
  // Shuts the pane (`_side=off`). The listing's search row grows the reopening
  // half of the affordance while the pane is down — SideChrome writes the split
  // between the two down.
  onClose: () => void;
}) {
  // Keep the keyboard on the listing when a companion iframe mounts (the pane's
  // focus contract — platform/lib/frame-focus.ts).
  const { rootRef, guardProps } = usePaneFocusGuard<HTMLDivElement>();

  // --- the pane's modes (listing/pane-side.ts) --------------------------------
  // A TAB STRIP over the two companions the pane may be on (SideChrome's
  // SideTabs — the same control the file sidebar's header wears), and never
  // fewer: an unofferable companion is drawn disabled with its reason, or
  // spinning while its probe is out (paneSideMenu), rather than dropped, so a
  // mount-backed folder's pane header still says what it cannot show.
  //
  // (`mcp` is not a pane mode — PANE_SIDE_COMPANIONS is the pair — but a dialog
  // off the search row's kebab, EntryActionsMenu → McpDialog.)
  //
  // Unlike before D460 this list HOLDS STILL as the selection moves, because
  // nothing here ever read the selection.
  const sideTabs = (
    <SideTabs
      tabs={paneSideMenu(sideEntries).map((e) => ({
        ...e,
        icon: paneSideIcon(e.mode, sideEntries),
      }))}
      active={side}
      onSelect={(m) => onSelectSide(m as PaneSideChoice)}
    />
  );

  // The pane's chrome strip, and EVERY state gets one — the loading skeleton
  // and the fallback hint alike. It is the TOP BAR of the right-hand column
  // now that the pane runs the full height of the window, so it has to hold
  // its height in every state or the seam it shares with the crumb bar on the
  // left breaks (see .pane-header).
  //
  // It opens with the way OUT of the column and it ends with the tab strip,
  // which is the file sidebar's header exactly (SideChrome, PreviewSidebar) —
  // the two columns are the same column over a folder and over a file, so they
  // wear the same bar. "Open in project" no longer rides in here: it is a row of
  // the search row's kebab, which is there whether or not this column is.
  const strip = () => (
    <div className="pane-header">
      <SideCloseButton what={modeTitle(side)} onClick={onClose} />
      <div className="side-header-tail">{sideTabs}</div>
    </div>
  );

  // --- no mode yet -----------------------------------------------------------
  if (undecided) {
    return (
      <div className="listing-pane">
        {strip()}
        <div className="pane-skel" />
      </div>
    );
  }

  // --- Claude, Git and MCP: the companions -----------------------------------
  // All of them render straight from the FOLDER's entry — `_file` is always
  // `folder` (D460; it used to be the selected row for `claude`, folder for
  // the other two — paneSideTarget, deleted, made that distinction and no
  // longer needs to).
  const sideEntry = side === "preview" ? null : sideEntries[side];
  if (sideEntry && sideEntry.path !== null) {
    // `chat_only=1` takes away the chat template's OWN left preview pane — the
    // rule and its two reasons are on paneChatOnly. `_remote` never applied
    // here: git/mcp's gates refuse a mount-backed directory outright, and
    // claude reads through the server either way.
    const chatOnly = paneChatOnly(side) ? CHAT_ONLY_PARAM : "";
    // No mention of the git companion's "Fix with AI" prompt here any more
    // (review #804 round 2): it is not a param this src carries at all — the
    // claude template PULLS it at its own boot instead (Listing.tsx's
    // `_fusedClaudeAskTake`), and this component's `key` (Listing.tsx, folded
    // with `claudeAskInstance` for exactly the claude case) is what makes sure
    // a fresh ask gets a fresh mount to pull it into.
    // `_noopen=1`, not `_preview=1` (D622): this pane is fully interactive —
    // you type in it — so it must not carry the display-only stamp
    // `runtime.js`'s `IS_THUMBNAIL` reads off `_preview`, which would silently
    // disable `fused.daemon.*` for every app it frames (its own left preview
    // pane included, two levels down). `_noopen=1` says only the one thing this
    // call site actually wants: don't record this render as an app open.
    // The URL shape itself lives in `apps/claude/legacy-src.ts` with the other
    // five and the byte-for-byte parity test that pins all six; `withNoFocus`
    // stays here because it is a fact about this HOST, not about the address.
    const src = withNoFocus(listingPaneSrc(sideEntry.path, folder, chatOnly));
    // The claude companion is the one whose document restores a transcript
    // before it has anything to show, so it is the one framed behind a cover
    // that waits for `data-chat-ready` (platform/ui/ChatFrame — same as the
    // Cards wall and its popup). git and mcp paint their own first frame and
    // stamp nothing, so they stay plain: a cover revealed only by the 8s
    // fallback would be a new wait, not a fix.
    return (
      <div className="listing-pane" ref={rootRef} {...guardProps}>
        {strip()}
        {side === "claude" ? (
          // FLAG ON the native chat renders here instead, `chat_only` for the
          // reason `paneChatOnly` gives, and its params live on the SHELL URL
          // (this pane is the page, not a card). `_nofocus` rides the legacy src
          // via `withNoFocus`; natively it is `noFocus`, which is what stops the
          // pane taking the keyboard off the listing.
          <ChatMount
            legacySrc={src}
            className="pane-frame"
            title={modeTitle(side)}
            file={folder}
            // The same one answer the src fragment is built from — asked once
            // (pane-modes.test pins that: one literal, one call).
            chatOnly={!!chatOnly}
            noFocus
            noOpen
            paramsSource="url"
            {...(initialAsk ? { initialAsk } : {})}
          />
        ) : (
          <iframe className="pane-frame" src={src} title={modeTitle(side)} />
        )}
      </div>
    );
  }

  // --- the fallback: no companion is offered ----------------------------------
  // A mount-backed folder, where all three gates refuse (claude, git AND
  // mcp — paneSideList only falls back here once every one of them has
  // answered no). The pane must still show something, and since D460 that
  // something is a plain folder-scoped hint — there is no selected row left
  // to resolve a template or a metadata card for. The switcher above still
  // draws all three companions, disabled and explained (paneSideMenu), which
  // is the honest account of why this is what the pane has to say.
  return (
    <div className="listing-pane">
      {strip()}
      <div className="pane-center">
        <div className="pane-hint">No companion is available for this folder.</div>
      </div>
    </div>
  );
}
