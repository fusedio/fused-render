// THE NARROW SINGLE-VIEW LAYOUT (T:8847-8990 `applyNarrowView`).
//
// Below 800px the split becomes ONE view at a time with a toggle. The breakpoint
// is derived from the columns' minimum USEFUL widths, not from where the split
// stops overflowing (T:3690-3717): a 420px framed preview + a 4px divider + 440px
// of transcript prose ≈ 864px, and the breakpoint sits deliberately a little
// BELOW that floor, trading a slightly-squeezed band (800–864) for keeping the
// split alive on more hosts.
//
// It lives with the chat rather than in the shell because a pane's width is
// DYNAMIC, which makes a shell-side "filter split modes out of a narrow pane"
// wrong in both directions — and would make the mode appear and disappear from
// the switcher mid-drag (T:3725-3737).
import { useCallback, useEffect, useRef, useState } from "react";
import type { ParamsStore } from "../params/store";

/**
 * The one definition of the breakpoint, matched with `matchMedia` rather than an
 * `innerWidth` comparison: `innerWidth < 800` and the media query disagree about
 * a scrollbar's width, and that disagreement is a half-collapsed layout
 * (T:5888-5891).
 */
export const NARROW_MQ_QUERY = "(max-width: 800px)";

/** Which of the two narrow views is on screen. */
export type NarrowView = "chat" | "preview";

/**
 * Which view a narrow layout shows, given the param, a live breakpoint crossing,
 * and the default (T:8868).
 *
 * Default CHAT: an unset param reads as the chat, so a narrow pane opens on the
 * conversation — the reason the mode exists — and the preview is one click away.
 * `crossView` sits between the param and that default: a live crossing keeps the
 * preview on screen without spending a history entry on a resize.
 */
export function narrowViewOf(param: string | undefined, crossView: NarrowView | null): NarrowView {
  return (param || crossView || "chat") === "preview" ? "preview" : "chat";
}

/** The button names its DESTINATION, and names what the destination is FOR: the
 *  preview column is where the annotation tools live, which is the only reason
 *  to leave the conversation for it ("Preview" said where you would land and not
 *  why). ONE string for the label and the aria-label (T:8880-8885). */
export function viewToggleLabel(view: NarrowView): string {
  return view === "preview" ? "Back to chat" : "Comment on preview";
}

/** `body.view-*` in T; here the classes go on `.chat-root`, which is the native
 *  shell's equivalent scope — `body` belongs to the whole app now (T:8869). */
export function viewClassNames(narrow: boolean, view: NarrowView, noPane: boolean): string {
  if (noPane) return "";
  const parts: string[] = [];
  if (narrow) parts.push("narrow");
  parts.push(view === "preview" ? "view-preview" : "view-chat");
  return parts.join(" ");
}

export interface UseNarrowViewOptions {
  params: ParamsStore;
  /** `enterNoPane` has run: `paneview` describes a collapse this target never
   *  does, so no class is written at all (T:8862). */
  noPane: boolean;
  /**
   * ARRIVING AT THE NARROW CHAT VIEW DISARMS annotate mode (T:8887-8930) — by
   * flipping there from Preview, or by the pane crossing 800px while the chat is
   * what shows. The media rules hide the annotate toggle in the chat-only view
   * because there is no frame to point at; leaving the mode armed behind a
   * hidden toggle would keep the frame's capture-phase click swallower live over
   * a document the user cannot see, in a state its own view cannot undo. PR3
   * passes `annSetMode(false)`; unset is a no-op.
   *
   * A CAPTURED pane shot is deliberately NOT reset with it: an armed mode is an
   * invisible promise about a document the user can no longer see, a chip is a
   * picture that already exists of a pane that was visible when it was taken.
   */
  onArriveChat?: () => void;
  /** Injected for tests. */
  matchMedia?: (q: string) => MediaQueryList;
  /** Called after every view/breakpoint change. T calls `renderAnn()` twice here
   *  — now, because reading a rect flushes THIS document's layout, and on the
   *  next frame, because the framed document only reflows to the iframe's new
   *  box after that (T:8948-8950). PR3 wires it. */
  onRemeasure?: () => void;
}

export interface NarrowViewState {
  /** `NARROW_MQ.matches`. */
  narrow: boolean;
  /** The view ON SCREEN, which is not always what the param says. */
  view: NarrowView;
  /** `"narrow view-preview"` / `"narrow view-chat"` / `""`, for `.chat-root`. */
  classNames: string;
  /** The toggle's one string. */
  label: string;
  /** Flip the view. Writes the PARAM only, the same one-way flow `split` and
   *  `leftmode` use. */
  toggle: () => void;
  /**
   * Whether the composer is out of reach in this view. In `view-preview` the
   * chat column collapses to its control strip (`#chat > *:not(#anntools)` is
   * display:none, T:3879) — the composer, the transcript and the send button are
   * all off screen, so a host that keys "can the user type" off this must not
   * offer a draft it cannot show. The CHIPS are not part of that: they are chat
   * content (the notes about to be sent, in the user's own words), and hiding
   * them would hide part of the message (T:3820-3826).
   */
  composerLocked: boolean;
}

export function useNarrowView(opts: UseNarrowViewOptions): NarrowViewState {
  const { params, noPane } = opts;
  const mq = useRef<MediaQueryList | null>(null);
  if (mq.current === null) {
    const match = opts.matchMedia ?? (typeof window !== "undefined" ? window.matchMedia : undefined);
    mq.current = match ? match.call(globalThis, NARROW_MQ_QUERY) : null;
  }

  const [narrow, setNarrow] = useState<boolean>(() => !!mq.current?.matches);
  const [param, setParam] = useState<string | undefined>(() => params.get("paneview"));
  /**
   * Crossing DOWN with `paneview` unset: both halves were on screen, and the
   * narrow default (chat) would here hide the preview the user was just looking
   * at, mid-resize, with no click. A VARIABLE, deliberately not a param write —
   * a resize is not a navigation, and the store's first-change push would mint a
   * history entry for it, so Back from a pristine visit would clear the param
   * and jump the layout to chat (Bugbot PR #447). Never persisted (T:8855, 8975).
   */
  const crossView = useRef<NarrowView | null>(null);
  /** The view that was on screen at the end of the last pass. `null` means BOOT,
   *  and only boot — see the disarm guard below (T:8851). */
  const shown = useRef<NarrowView | null>(null);

  useEffect(() => params.onChange((all) => setParam(all.paneview)), [params]);

  // The two callbacks are read through refs: their owners rebuild them every
  // render, and a pass is about the view/breakpoint pair, not about identity.
  const cbs = useRef({ onArriveChat: opts.onArriveChat, onRemeasure: opts.onRemeasure });
  cbs.current = { onArriveChat: opts.onArriveChat, onRemeasure: opts.onRemeasure };

  useEffect(() => {
    const m = mq.current;
    if (!m) return;
    const onChange = () => {
      if (m.matches && !noPane && !params.get("paneview")) crossView.current = "preview";
      setNarrow(m.matches);
    };
    m.addEventListener("change", onChange);
    return () => m.removeEventListener("change", onChange);
  }, [noPane, params]);

  const view = narrowViewOf(param, crossView.current);

  // The disarm, and the re-measure. Three clauses, one per thing that must not
  // trigger the disarm (T:8895-8928):
  //   * `view === "chat"` — disarming while Preview is on screen would fight a
  //     user arming the mode there, the one view where arming it is right;
  //   * `narrow` — above the breakpoint both halves are on screen, nothing is
  //     hidden behind anything, and armed is correct;
  //   * `shown.current !== null` — BOOT, and only boot. A URL that arrived
  //     carrying an explicit `annmode=1` keeps it: the param outlives the narrow
  //     host and belongs to the wide layout too. Deliberately NOT
  //     `shown.current === "preview"` — that narrower test describes a
  //     Preview→Chat FLIP and nothing else, and a media CROSSING is not a flip.
  useEffect(() => {
    if (noPane) return;
    if (narrow && shown.current !== null && view === "chat") cbs.current.onArriveChat?.();
    shown.current = view;
    cbs.current.onRemeasure?.();
    const raf =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame(() => cbs.current.onRemeasure?.())
        : null;
    return () => {
      if (raf !== null) cancelAnimationFrame(raf);
    };
  }, [narrow, view, noPane]);

  const toggle = useCallback(() => {
    // The opposite of the view ON SCREEN, not of the raw param: after a
    // breakpoint crossing the preview can be showing on `crossView` with the
    // param still absent, and reading the param there would write "preview" over
    // a visible preview — a toggle whose first click does nothing (T:8946-8956).
    params.set({ paneview: shown.current === "preview" ? "chat" : "preview" }, { history: "replace" });
  }, [params]);

  return {
    narrow,
    view,
    classNames: viewClassNames(narrow, view, noPane),
    label: viewToggleLabel(view),
    toggle,
    composerLocked: narrow && !noPane && view === "preview",
  };
}
