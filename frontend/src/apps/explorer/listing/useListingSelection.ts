// Selection state + keyboard navigation for the listing: the multi-row
// selection model, the document-level arrow/Home/End/PageUp/Enter handler,
// the post-mutation reconcile (re-anchor by path), and scroll-into-view.
import { useEffect, useMemo, useRef, useState } from "react";
import { navigate, replaceSearch } from "@platform/lib/router";
import { isMod } from "@platform/lib/platform";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import type { RowCtx } from "@apps/explorer/listing/types";
import {
  EMPTY_SELECTION,
  oneSelected,
  pageRows,
  rangeBetween,
  recallSelection,
  rememberSelection,
  type Selection,
} from "@apps/explorer/listing/selection";

export function useListingSelection({
  fsPath,
  navRows,
  listingLoaded,
  searchInputRef,
  rowCtxByPathRef,
  overlayOpenRef,
  globalKeys = true,
  urlSync = true,
}: {
  fsPath: string;
  // Flat, ordered list of the paths the arrow keys step through (the rendered
  // order — the active sort or search ranking).
  navRows: string[];
  // Whether navRows reflects a LOADED listing (not a transient empty while the
  // fetch is in flight) — see Listing.tsx, where this is derived.
  listingLoaded: boolean;
  searchInputRef: React.RefObject<HTMLInputElement>;
  // Path -> RowCtx for the rendered rows, read by the once-registered keydown
  // handler so Enter can pass the row's is_dir as a nav hint.
  rowCtxByPathRef: React.MutableRefObject<Map<string, RowCtx>>;
  // True while a context menu or a modal dialog is open in this view. The
  // document-level nav handler hard-guards on this so an open overlay owns the
  // keyboard — a stray Enter can't navigate a row behind the dialog.
  overlayOpenRef: React.MutableRefObject<boolean>;
  // False for an EMBEDDED Listing (the preview pane's `_listing` mode): the
  // document-level keyboard belongs to the host view's own Listing, so the
  // embedded one keeps mouse selection but registers no global handlers.
  globalKeys?: boolean;
  // False for an embedded Listing: the address bar belongs to the host view,
  // so the lead row is never mirrored to (or seeded from) `?sel`.
  urlSync?: boolean;
}) {
  // The selected rows (see Selection): one for a plain click / arrow move, many
  // for a Shift-range, Mod-click toggle or Select All. Seeded from the
  // cross-remount store so a selection made in the pre-stat provisional Listing
  // survives the swap to the resolved one (see recallSelection).
  const [sel, setSel] = useState<Selection>(() => recallSelection(fsPath));
  // The lead row — every place that used to read `selectedPath` (scroll-into-
  // view, reconcile, Enter/F2 targets) still works off this single path.
  const selectedPath = sel.lead;

  // Latest ordered list of navigable row paths + the current selection, read by
  // the document keydown handler (registered once, so it can't close over them).
  const navRowsRef = useRef<string[]>([]);
  navRowsRef.current = navRows;
  const selRef = useRef<Selection>(sel);
  selRef.current = sel;
  // Fast membership test for the row renderer (a Select All can hold thousands).
  const selectedSet = useMemo(() => new Set(sel.paths), [sel.paths]);
  // Mirror the selection into the cross-remount store so it's already there
  // when the resolved Listing mounts (the provisional one has no unmount step
  // that would clear it). Keyed by fsPath, so a real nav to another folder
  // starts fresh.
  useEffect(() => {
    rememberSelection(fsPath, sel);
  }, [fsPath, sel]);

  // URL sync for the lead row (like `preview`/`sort`): `?sel=<path relative
  // to this folder>` so a refreshed or shared URL restores the selection —
  // and with it the preview pane's content. navigate() drops the param on
  // folder navigation (only `preview` is sticky), so it never leaks.
  //   • Seed ONCE, after the listing has loaded: a recalled (cross-remount)
  //     selection outranks the URL, and a `sel` naming no current row is
  //     simply ignored (no forced selection).
  //   • Mirror only after the seed decision, so the initial no-selection
  //     render can't wipe the param before it was read.
  const urlSelSeededRef = useRef(false);
  useEffect(() => {
    if (!urlSync || urlSelSeededRef.current || !listingLoaded) return;
    urlSelSeededRef.current = true;
    const rel = new URLSearchParams(location.search).get("sel");
    if (!rel || selRef.current.paths.length) return;
    const abs = fsPath.replace(/\/+$/, "") + "/" + rel;
    if (navRows.includes(abs)) setSel(oneSelected(abs));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSync, listingLoaded, navRows, fsPath]);
  useEffect(() => {
    if (!urlSync || !urlSelSeededRef.current) return;
    const base = fsPath.replace(/\/+$/, "");
    const rel =
      sel.lead && sel.lead.startsWith(base + "/") ? sel.lead.slice(base.length + 1) : null;
    const params = new URLSearchParams(location.search);
    if (params.get("sel") === rel) return; // already in step (incl. both absent)
    if (rel !== null) params.set("sel", rel);
    else params.delete("sel");
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlSync, sel.lead, fsPath]);

  // A path the selection should jump to once it appears in the reloaded rows
  // (a rename/duplicate target — its row doesn't exist until the refetch lands).
  const pendingSelectRef = useRef<string | null>(null);
  // Last known index of the selection within navRows. When the selected path
  // vanishes (delete / move to bin / rename with no re-anchor) the reconcile
  // effect clamps to this slot so selection lands on the nearest surviving row.
  const lastSelIndexRef = useRef<number>(-1);

  // --- selection mutators ---------------------------------------------------
  // Every one of these closes over nothing but setSel and navRowsRef (both
  // stable for the component's life), so the once-registered document handlers
  // below can safely capture them from the first render.

  const selectOnly = (path: string) => setSel(oneSelected(path));

  const clearSelection = () => setSel(EMPTY_SELECTION);

  // Mod-click: add/remove one row, and make it the anchor a later Shift-range
  // pivots on (Finder/Explorer both re-anchor on the toggled row).
  const toggleSelected = (path: string) =>
    setSel((prev) => {
      if (!prev.paths.includes(path)) {
        return { paths: [...prev.paths, path], anchor: path, lead: path };
      }
      const paths = prev.paths.filter((p) => p !== path);
      // Deselecting the lead hands focus to whatever is left of the selection.
      return { paths, anchor: path, lead: paths.length ? paths[paths.length - 1] : null };
    });

  // Shift-click / Shift+arrow: the selection becomes anchor..path over the
  // RENDERED row order (navRows — the active sort or search ranking), with the
  // anchor left in place so further extension keeps pivoting on it.
  const extendTo = (path: string) =>
    setSel((prev) => {
      const anchor = prev.anchor ?? prev.lead;
      if (anchor === null) return oneSelected(path);
      const paths = rangeBetween(navRowsRef.current, anchor, path);
      if (!paths.length) return prev;
      return { paths, anchor, lead: path };
    });

  const selectAllRows = () =>
    setSel((prev) => {
      const rows = navRowsRef.current;
      if (!rows.length) return prev;
      return {
        paths: [...rows],
        anchor: prev.lead ?? rows[0],
        lead: prev.lead ?? rows[rows.length - 1],
      };
    });

  // Move the lead to `index` (clamped into the row range), either collapsing the
  // selection onto that row or extending the range from the anchor.
  const moveLeadTo = (index: number, extend: boolean) =>
    setSel((prev) => {
      const rows = navRowsRef.current;
      if (!rows.length) return prev;
      const next = rows[Math.max(0, Math.min(rows.length - 1, index))];
      if (!extend) return oneSelected(next);
      const anchor = prev.anchor ?? prev.lead ?? next;
      return { paths: rangeBetween(rows, anchor, next), anchor, lead: next };
    });

  // Keyboard navigation for the listing, whether focus is in the search box or
  // nowhere in particular:
  //   • a plain printable key focuses the search box so the character lands there;
  //   • Up/Down move the selection through the rendered rows — in the search box
  //     too, since a single-line input doesn't need them for the caret — and
  //     Shift+Up/Down extend the range from the anchor instead;
  //   • Home/End jump to the first/last row, PageUp/PageDown move a viewport
  //     (both extend with Shift, like every list widget);
  //   • Mod+A selects every rendered row, Escape clears the selection;
  //   • Enter opens the lead row, or the top row when nothing is selected yet.
  // Modifier chords that are NOT selection movement (Mod+Up/Down = parent/open)
  // are deliberately left to the shortcut handler (see Listing.tsx).
  // Bound to `document` so it also drives the plain listing with nothing focused.
  useEffect(() => {
    if (!globalKeys) return;
    function onKeyDown(e: KeyboardEvent) {
      // While an IME is composing, Enter confirms a candidate and the arrows
      // move through the candidate list — never repurpose them for navigation.
      if (e.isComposing) return;
      // An open context menu / dialog owns the keyboard: don't let Enter open a
      // row behind it (the dialog handles its own Enter/Escape). isOverlayOpen()
      // also covers an overlay owned by a HOSTING view (Preview's header menu
      // when this Listing is embedded), which overlayOpenRef alone can't see.
      if (overlayOpenRef.current || isOverlayOpen()) return;
      const el = document.activeElement as HTMLElement | null;
      const inSearch = el === searchInputRef.current;
      // Only drive navigation from the search box or when nothing in particular
      // is focused (body). If focus is on a chrome control — a breadcrumb link,
      // the bookmark/mode-switch buttons, another input — leave its keys alone
      // (otherwise Enter would open a file instead of activating that control).
      const navActive =
        inSearch || !el || el === document.body || el === document.documentElement;

      const rows = navRowsRef.current;
      const leadIdx = rows.indexOf(selRef.current.lead ?? "");

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!navActive) return;
        // Mod+Up/Down are navigation chords (parent folder / open), owned by the
        // shortcut handler — they must not also move the selection.
        if (isMod(e) || e.altKey) return;
        if (!rows.length) return;
        e.preventDefault();
        const down = e.key === "ArrowDown";
        // Nothing selected yet: Down starts at the top, Up at the bottom.
        const next = leadIdx === -1 ? (down ? 0 : rows.length - 1) : leadIdx + (down ? 1 : -1);
        moveLeadTo(next, e.shiftKey);
        return;
      }
      if (e.key === "Home" || e.key === "End") {
        // Unlike Up/Down, Home/End are real caret navigation in a text field, so
        // the search box keeps them (same carve-out as Mod+A and Escape below).
        if (!navActive || inSearch || isMod(e) || !rows.length) return;
        e.preventDefault();
        moveLeadTo(e.key === "Home" ? 0 : rows.length - 1, e.shiftKey);
        return;
      }
      if (e.key === "PageDown" || e.key === "PageUp") {
        if (!navActive || isMod(e) || !rows.length) return;
        e.preventDefault();
        const step = pageRows();
        const down = e.key === "PageDown";
        const next = leadIdx === -1 ? (down ? 0 : rows.length - 1) : leadIdx + (down ? step : -step);
        moveLeadTo(next, e.shiftKey);
        return;
      }
      if (isMod(e) && e.key.toLowerCase() === "a") {
        // Select All. In the search box it must keep meaning "select the text",
        // otherwise clearing a typed query becomes impossible.
        if (!navActive || inSearch || !rows.length) return;
        e.preventDefault();
        selectAllRows();
        return;
      }
      if (e.key === "Escape") {
        // Clear the selection. The search input owns Escape while focused (it
        // clears the query — see its onKeyDown), and the overlay/dialog guards
        // above already stopped us if anything modal is up.
        //
        // A pending copy/cut outranks the selection: App's capture-phase Escape
        // handler cancels the clipboard and calls preventDefault(), so one press
        // never does both. Reading defaultPrevented keeps that precedence here
        // without a second copy of the clipboard logic (which would also be
        // wrong — the cancel has to work from Preview, where no Listing exists).
        if (e.defaultPrevented) return;
        if (!navActive || inSearch) return;
        if (!selRef.current.paths.length) return;
        e.preventDefault();
        clearSelection();
        return;
      }
      if (e.key === "Enter") {
        // Already consumed by a chrome control that unmounted itself on the
        // way (the breadcrumb's path input commits and closes on Enter, which
        // hands focus back to <body> before this listener runs — navActive
        // alone can't see that the key was spoken for).
        if (e.defaultPrevented) return;
        if (!navActive) return;
        if (!rows.length) return;
        e.preventDefault();
        const target = leadIdx === -1 ? rows[0] : rows[leadIdx];
        navigate(target, { isDir: rowCtxByPathRef.current.get(target)?.isDir });
        return;
      }
      // Start typing → focus the search box so the character lands there. Only
      // when nothing else is focused (not the search box already, not a chrome
      // control) and only plain printable keys (no modifiers), so Space on a
      // focused button and app shortcuts keep working.
      if (
        navActive && !inSearch &&
        e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey
      ) {
        searchInputRef.current?.focus(); // keystroke falls through into the input
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [globalKeys]);

  // Keep the keyboard selection scrolled into view as it moves. Follows the LEAD
  // row (`.lead`), not merely the first selected one: extending a Shift-range
  // downward must keep the moving end visible, and the top of the range is
  // usually the one that would otherwise win a `.selected` query.
  // This is also what keeps the selection in view across a SORT: navRows is
  // a dependency, and a sort click produces a new navRows.
  useEffect(() => {
    if (!selectedPath) return;
    (
      document.querySelector("table.listing-table tr.row.lead") ??
      document.querySelector("table.listing-table tr.row.selected")
    )?.scrollIntoView({ block: "nearest" });
  }, [selectedPath, navRows]);

  // Re-anchor the selection by PATH whenever the rows change (a refetch after
  // rename / duplicate / delete / move-to-bin) or the selection moves. Without
  // this the selected index kept pointing at the OLD name after a rename, so
  // pressing Enter opened a path that no longer existed.
  //   • A pending re-anchor (rename/duplicate target) is adopted the moment its
  //     row appears in the reloaded listing.
  //   • A still-present selection just refreshes its remembered slot index.
  //   • A vanished selection (deleted / trashed / moved) clamps to the nearest
  //     surviving row (or clears when the folder is now empty).
  // The pending wait is BOUNDED, not open-ended: it only holds while the current
  // selection is itself a live row. Renaming a search hit whose new path isn't a
  // search match leaves the pending target absent from navRows forever while the
  // old selected path also disappears — waiting unconditionally there would
  // strand the selection on a dead row (broken Enter). So once the old selection
  // is gone too, the pending target is abandoned and the normal clamp runs. The
  // pending path still lands the moment it does appear (e.g. search results
  // refetching to include the renamed file), so the happy path is unchanged.
  //   • Rows of a MULTI-selection that vanished are pruned while the lead
  //     survives, so a batch op that partly failed doesn't leave dead paths in
  //     the selection (and a later Cmd+C can't copy them).
  useEffect(() => {
    const rows = navRows;
    const pend = pendingSelectRef.current;
    let clampFallback = false;
    if (pend !== null) {
      const pi = rows.indexOf(pend);
      if (pi !== -1) {
        pendingSelectRef.current = null;
        lastSelIndexRef.current = pi;
        if (selectedPath !== pend || sel.paths.length !== 1) setSel(oneSelected(pend));
        return;
      }
      // Target not here yet. Keep waiting ONLY while the current selection is
      // still a real row (nothing's broken, the target may still arrive). If it
      // has also vanished, give up on the pending target and clamp below.
      if (selectedPath !== null && rows.indexOf(selectedPath) !== -1) return;
      pendingSelectRef.current = null;
      clampFallback = true;
    }
    if (selectedPath === null) {
      // No selection to reconcile. Only force one when a pending target was just
      // abandoned (so selection never stays dead); otherwise leave it unset.
      if (!clampFallback || rows.length === 0) return;
      const clamped = Math.min(Math.max(lastSelIndexRef.current, 0), rows.length - 1);
      setSel(oneSelected(rows[clamped]));
      return;
    }
    const i = rows.indexOf(selectedPath);
    if (i !== -1) {
      lastSelIndexRef.current = i; // lead still valid; remember its slot
      // Drop any other selected rows that are gone (deleted/moved/renamed).
      if (sel.paths.length > 1) {
        const live = new Set(rows);
        const kept = sel.paths.filter((p) => live.has(p));
        if (kept.length !== sel.paths.length) {
          setSel({
            paths: kept,
            anchor: sel.anchor !== null && live.has(sel.anchor) ? sel.anchor : selectedPath,
            lead: selectedPath,
          });
        }
      }
      return;
    }
    // Selection isn't in the current rows. While the listing is still LOADING
    // (rows transiently empty during a fetch — notably the pre-stat provisional
    // Listing being swapped for the resolved one right after a folder opens),
    // don't treat it as vanished: keep it and rerun once rows arrive. Clearing
    // here is what dropped an arrow-key selection made just after opening a
    // folder, even with the selection carried across the remount.
    if (!listingLoaded) return;
    if (rows.length === 0) {
      setSel(EMPTY_SELECTION);
      return;
    }
    const clamped = Math.min(Math.max(lastSelIndexRef.current, 0), rows.length - 1);
    setSel(oneSelected(rows[clamped]));
  }, [navRows, selectedPath, sel, listingLoaded]);

  return {
    sel,
    selectedPath,
    selectedSet,
    selectOnly,
    toggleSelected,
    extendTo,
    clearSelection,
    pendingSelectRef,
  };
}
