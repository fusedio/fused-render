// THE LEFT PANE — the target's ordinary preview, framed (T:5838-5884 `paneReady`,
// plus `applyLeftMode` T:5669-5702 and `enterNoPane` T:5704-5836).
//
// Framed via `/render`, never `/embed`, and never nested one iframe deeper: the
// frame's `contentDocument` IS the app's document, which is what lets app-state
// read it and (PR3) lets the annotation layer wire the document a click lands in
// (T:5248-5258, T:4858-4877). There is NO postMessage anywhere in this
// subsystem, in either direction (D3/D4).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "../styles/pane.css";
import { statPath, type StatResult } from "@platform/lib/api";
import { runAppEntry, resolveAgentDir } from "../protocol/agent";
import type { ParamsStore } from "../params/store";
import { LeftModePicker, leftBarShown } from "./LeftModePicker";
import type { NarrowViewState } from "./useNarrowView";
import type { AppStateWatcher } from "./appState";
import {
  curLeftEntry,
  decidePane,
  paneSrcFor,
  type PaneDecision,
  type PaneNoun,
  type PaneSrcFlags,
  type TargetNoun,
} from "./paneUrl";

// ── enterNoPane: a designed ABSENCE, not a missing element ────────────────────
//
// THE HAZARD THIS SHAPE EXISTS TO AVOID (T:5704-5761). The obvious template
// implementation — ship the markup without the frame for this target — could not
// work and failed in a way that hid itself: the frame's `load` hook was wired at
// top level, so with no element to wire that statement threw and aborted every
// declaration after it, and the boot catch could not report it either because
// its first statement was `.remove()` on the same missing element. A blank page
// with a working-looking composer.
//
// React removes the whole class of failure — a component that is not rendered
// wires nothing — so step 6 below is simply "do not render". What does NOT go
// away is the ORDER of the other five, because both of the first two undo
// something BOOT already did on the strength of a param, before anyone knew
// there was no pane. Both were live bugs found by opening a folder URL that
// still carried an old bookmark's `annotations` and `paneview=preview`.
//
// THE ORDER, verbatim (T:5762-5836):
//
//   1. Drop the notes and repaint, WHILE `noPane` is still false so the repaint
//      actually runs. The boot repaint had ALREADY painted a chip per composer
//      from the `annotations` param, and a later `noPane` early-return would
//      have frozen it on screen — a chip showing a note that cannot be sent is a
//      worse lie than no chip. The PARAM is left alone: this drops the list, it
//      does not rewrite the URL.
//   2. Disarm annotate mode THROUGH `annSetMode`, the one door in and out, so
//      the label, `aria-pressed`, the param and the pins stay in agreement. With
//      `noPane` set the call is a plain disarm that writes no param.
//   3. Drop the narrow layout's view class, which boot stamped from `paneview`.
//      It has to GO rather than be left inert: `view-preview` collapses the chat
//      column to its control strip, and with the strip slimmed and no pane to
//      show instead, a narrow host would render a blank page.
//   4. Stamp the no-pane class, which hands the kebab the strip's auto margin —
//      the annotate switch, its usual owner, is about to leave.
//   5. Rescue the note composer BEFORE the column it lives in goes. CHAT_ONLY
//      only: close it FIRST (a reader who clicked the pane in the gap between
//      the stat fetch and this teardown has an open, portaled composer, and
//      yanking a live one across documents would leave it visible at a
//      coordinate that meant something in someone else's viewport), then park it
//      under the control strip.
//   6. Remove the pane's controls — everything that acts on the column being
//      taken away. The KEBAB STAYS: its one item acts on the TARGET, and a
//      folder with no preview is still a thing to open a terminal session on.
//      CHAT_ONLY keeps the annotate switch, the recorder, the pin-kind picker
//      and the screenshot button, because those act on the HOST's pane, which
//      that layout still has.
//
// CHAT_ONLY takes this same path (the decision answers `kind: "none"` for every
// kind there) and it is the one caller for which "no pane of ours" is NOT
// "nothing to annotate" — hence steps 1, 2 and 5's exceptions.
//
// What this deliberately does NOT do is tidy the URL: `split`, `paneview`,
// `leftmode`, `annmode` and `annotations` left by an old bookmark are IGNORED,
// silently — the same forgiving posture PT-9 takes for an unknown `_mode`.
// Deleting them would break the bookmark's round trip for the day that folder
// becomes an app and gets its pane back.

export interface NoPaneSteps {
  /** `chat_only=1` — the host's pane is still on screen. */
  chatOnly: boolean;
  /** Step 1a (PR3): empty the in-memory notes list. Skipped in CHAT_ONLY. */
  clearAnnotations?: () => void;
  /** Step 1b (PR3): repaint, while `noPane` is still false. */
  renderAnn?: () => void;
  /** Step 1c: latch `noPane`. */
  setNoPane: () => void;
  /** Step 2 (PR3): the one door out of annotate mode. Skipped in CHAT_ONLY. */
  annSetMode?: (on: boolean) => void;
  /** Steps 3 + 4 are class writes; in React they fall out of `noPane` (see
   *  `useNarrowView`, which returns `""` for a no-pane target, and the chat
   *  root's own `nopane` class). Passed only so a host that needs to act on the
   *  transition can. */
  onLayoutDropped?: () => void;
  /** Step 5 (PR3, CHAT_ONLY only): close the portaled composer, then park it. */
  rescueComposer?: () => void;
}

/** Runs steps 1-5 in T's order. Step 6 is "the pane's controls are not
 *  rendered", which is the parent's `noPane` branch. */
export function enterNoPane(steps: NoPaneSteps): void {
  if (!steps.chatOnly) steps.clearAnnotations?.();
  steps.renderAnn?.();
  steps.setNoPane();
  if (!steps.chatOnly) steps.annSetMode?.(false);
  steps.onLayoutDropped?.();
  if (steps.chatOnly) steps.rescueComposer?.();
}

// ── the decision, resolved ───────────────────────────────────────────────────

export type PaneStatus = "resolving" | "ready" | "none" | "error";

export interface PaneState {
  status: PaneStatus;
  decision: PaneDecision | null;
  /** The message the error panel shows ("Could not open preview: …"). */
  error: string | null;
  /** `enterNoPane` has run (or the target never had a pane). */
  noPane: boolean;
  noun: TargetNoun;
  paneNoun: PaneNoun;
  /**
   * `paneReady` — resolves once the pane layout is decided, and NEVER rejects
   * (T:5838-5844). Held as a promise rather than merely fired because the
   * landing page needs `noun` — the page's one answer to "what kind of thing is
   * this chat about" — before it can decide whether to show the snapshots panel.
   * Awaiting it is only ever a wait, not a second failure path.
   */
  ready: Promise<void>;
}

export interface UsePaneStateOptions {
  /** `_file`. `null` (no target) resolves to no pane. */
  file: string | null;
  chatOnly: boolean;
  /** The claude template's folder, for `./app.py`. Resolved from `file` when
   *  absent (protocol/agent `resolveAgentDir`). */
  agentDir?: string | null;
  /** Shell-mounted framing flags for the iframe src. */
  flags?: PaneSrcFlags;
  /** `leftmode` at decision time — which offerable view a FILE target opens in.
   *  Later changes go through `useFramedSrc`, never a re-decide. */
  initialLeftMode?: string;
  /** Steps 1, 2 and 5 of `enterNoPane` (PR3 wires them). */
  noPaneSteps?: Omit<NoPaneSteps, "chatOnly" | "setNoPane">;
  /** T's `noPane` LATCH, shared. Step 1 has to run while it is still false (the
   *  repaint must actually repaint) and every later writer checks it, so the
   *  flag is a ref a caller can hand in rather than a piece of React state that
   *  lands a tick later. Left unset it is internal. */
  noPaneFlag?: { current: boolean };
  /** So the pane's own error reaches the model: "the pane is showing an error
   *  instead of your app" is the most important thing the state channel can
   *  carry, and from here the frame is gone so nothing else will report it
   *  (T:5879-5883). */
  watcher?: AppStateWatcher | null;
}

export function usePaneState(opts: UsePaneStateOptions): PaneState {
  const { file, chatOnly } = opts;
  // "RESOLVING" IS A CLAIM, and for two targets it is a false one: CHAT_ONLY has
  // no pane of ours whatever the stat says (`decidePane` answers `kind: "none"`
  // for every kind there) and a null `file` has no target to stat. Both used to
  // spend a round-trip in "resolving" — a status the page then read as "a pane
  // is coming", which is how `has_pane: 1` reached the model on the first send
  // of a chat that never had one. The one thing still in flight for CHAT_ONLY is
  // the NOUN (the placeholder and the footnote name the target), and that fills
  // in below without ever moving the status off "none".
  const noPaneTarget = chatOnly || !file;
  const [state, setState] = useState<Omit<PaneState, "ready">>(() => ({
    status: noPaneTarget ? "none" : "resolving",
    decision: null,
    error: null,
    noPane: noPaneTarget,
    noun: "",
    paneNoun: "preview",
  }));

  // One promise per target, resolved exactly once, never rejected.
  const gate = useRef<{ file: string | null; promise: Promise<void>; done: () => void } | null>(null);
  if (!gate.current || gate.current.file !== file) {
    let done = () => {};
    const promise = new Promise<void>((res) => {
      done = res;
    });
    gate.current = { file, promise, done };
  }
  const settle = gate.current.done;

  const live = useRef(opts);
  live.current = opts;
  const ownFlag = useRef(false);

  // THE STATE DESCRIBES ONE TARGET, and a hop to another must not leave the
  // previous one's answers standing while the new stat is out (Bugbot, PR
  // #1061). `gate.current.promise` is already per-target, so anything that
  // AWAITS `ready` was always correct; `status`, `decision` and `noPane` are
  // read SYNCHRONOUSLY, and holding them on the old file cost two things:
  //
  //  - the pane went on framing the OLD file's preview (`decision.src`) until
  //    the round trip landed — for a file hop, someone else's document under
  //    this conversation's header; and
  //  - `has_pane` answered TRUE off a stale `status: "ready"`, which bypasses
  //    the `"resolving" → null` mitigation below. That guard only ever
  //    protected the FIRST target — the one whose state STARTS OUT resolving —
  //    so the race it exists to prevent came back on every hop, and with it the
  //    price the comment there spells out: a session spawned with no
  //    `mcp__fused_approvals__app_state` and no way back.
  //
  // Hosts that remount per file never saw this (the explorer's `ChatMount` is
  // keyed, and PreviewSidebar remounts on every hop); one that swaps `file` in
  // place — `agentDir` already cached, so the tree is not rebuilt either — does.
  //
  // RESET IN THE RENDER that first sees the new `file`, not in the effect: an
  // effect lands after a paint, and that paint is exactly the stale frame.
  const stateFor = useRef(file);
  if (stateFor.current !== file) {
    stateFor.current = file;
    // The latch RE-ARMS with the target. It exists to keep ONE target's
    // no-pane steps from running twice (CHAT_ONLY enters before the stat and
    // the decision reaches the same branch after), never to keep the next
    // target from entering no-pane at all — left set, a hop from a paneless
    // target to another paneless one would skip the five steps entirely.
    (opts.noPaneFlag ?? ownFlag).current = false;
    setState({
      status: noPaneTarget ? "none" : "resolving",
      decision: null,
      error: null,
      noPane: noPaneTarget,
      noun: "",
      paneNoun: "preview",
    });
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const { agentDir, flags, initialLeftMode, noPaneSteps, watcher } = live.current;
      const flag = live.current.noPaneFlag ?? ownFlag;
      const setNoPane = () => {
        flag.current = true;
      };
      // The LATCH is the "already entered" test, which is what keeps the five
      // steps to one run: CHAT_ONLY enters no-pane before the stat and the
      // decision reaches the same branch afterwards, and step 5 (rescue the
      // portaled composer) must not run twice.
      const goNoPane = () => {
        if (!flag.current) enterNoPane({ chatOnly, setNoPane, ...noPaneSteps });
      };
      if (!file) {
        // No target, no pane. The template threw for a missing `_file` and
        // replaced the body with a message (T:4653-4656); the native shell's
        // hosts pass `null` legitimately (a cards tile with no file), so it is a
        // no-pane layout rather than an error page.
        if (!cancelled) {
          goNoPane();
          setState({ status: "none", decision: null, error: null, noPane: true, noun: "", paneNoun: "preview" });
          settle();
        }
        return;
      }
      // Latched BEFORE the round-trip, not after it: `has_pane` is read at SEND
      // time and the composer is live from the first paint, so a chat-only mount
      // must already know it has no pane of its own. Only the noun is awaited.
      if (chatOnly) goNoPane();
      try {
        const stat: StatResult = await statPath(file);
        let appEntry = null;
        if (stat.is_dir) {
          const dir = agentDir ?? (await resolveAgentDir(file));
          // No claude template folder means no `./app.py` to ask — which reads
          // as "not an app folder", the same answer an empty entry gives.
          appEntry = dir ? await runAppEntry(dir, file, { key: null }).catch(() => null) : null;
        }
        const decision = decidePane({ file, chatOnly, stat, appEntry, leftMode: initialLeftMode, flags });
        if (cancelled) return;
        watcher?.setEntry(decision.entry);
        if (decision.src === null) {
          goNoPane();
          setState({
            status: "none",
            decision,
            error: null,
            noPane: true,
            noun: decision.noun,
            paneNoun: decision.paneNoun,
          });
        } else {
          setState({
            status: "ready",
            decision,
            error: null,
            noPane: false,
            noun: decision.noun,
            paneNoun: decision.paneNoun,
          });
        }
      } catch (err) {
        if (cancelled) return;
        const message = (err as Error)?.message || String(err);
        // The frame is swapped for the message, but NOT the box around it: that
        // box also hosts the annotation layer, and wiping it would leave JS
        // driving detached nodes (T:5865-5868).
        // `noPane` is the LATCH's answer, not a constant: a CHAT_ONLY mount has
        // already entered no-pane above and a failed stat does not give it one.
        setState((prev) => ({ ...prev, status: "error", error: message, noPane: flag.current }));
        live.current.watcher?.pushLog("error", "the left pane could not open the preview: " + message);
      } finally {
        if (!cancelled) settle();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [file, chatOnly, settle]);

  return { ...state, ready: gate.current.promise };
}

/**
 * The iframe src for whatever `leftmode` now names (T:5669-5702 `applyLeftMode`).
 * Re-derived from the entry list stat already gave us — never a second stat.
 *
 * ORDERING DISCIPLINE, pinned by a test rather than left to be remembered:
 * `paneSrcFor` is resolved BEFORE `framedMode` is committed. It throws for an
 * offerable entry with no `path`, and `framedMode` is the record of what the
 * iframe IS showing — writing it first left it naming a mode the frame was never
 * pointed at, which then made the idempotence guard SKIP the retry and let the
 * throw escape the param listener, so the narrow-view pass and the pin repaint
 * were skipped for that tick.
 *
 * ANNOTATIONS SURVIVE THE SWAP, deliberately. Every pin is anchored to an
 * element of the framed document, so a new view invalidates the anchors — but
 * the notes live in the `annotations` param because they are the USER'S WORDS,
 * they are still perfectly sendable as text, and losing them to a click on a
 * view picker would be data loss with no warning. So the list stays and only the
 * POSITIONS are recomputed, on the frame's own `load`.
 */
export function useFramedSrc(
  decision: PaneDecision | null,
  leftMode: string | undefined,
  file: string | null,
  flags?: PaneSrcFlags,
): { src: string | null; framedMode: string | null; error: string | null } {
  // The record of what the frame is showing, so an unchanged mode is not a
  // re-navigation (the param store's `onChange` fires for every param, `split`
  // included).
  const framed = useRef<{ mode: string | null; src: string | null }>({ mode: null, src: null });
  // DERIVED IN THE RENDER, COMMITTED IN AN EFFECT. Writing the record inside the
  // memo meant a render React discarded still moved it, and the idempotence
  // guard below then short-circuited the swap that really happened — the frame
  // kept showing the old view with the picker saying otherwise.
  const next = useMemo(() => {
    if (!decision || decision.src === null) {
      return { src: null, framedMode: null, error: null, commit: { mode: null, src: null } };
    }
    if (decision.kind !== "file" || decision.leftModes.length === 0) {
      return {
        src: decision.src,
        framedMode: decision.framedMode,
        error: null,
        commit: { mode: decision.framedMode, src: decision.src },
      };
    }
    const t = curLeftEntry(decision.leftModes, leftMode);
    if (!t || t.mode === framed.current.mode) {
      return {
        src: framed.current.src ?? decision.src,
        framedMode: framed.current.mode,
        error: null,
        commit: null,
      };
    }
    try {
      const src = paneSrcFor(t, file ?? "", decision.remote, flags);
      return { src, framedMode: t.mode, error: null, commit: { mode: t.mode, src } };
    } catch (err) {
      // Nothing committed: the frame keeps showing what it was showing.
      return {
        src: framed.current.src ?? decision.src,
        framedMode: framed.current.mode,
        error: (err as Error).message,
        commit: null,
      };
    }
  }, [decision, leftMode, file, flags]);
  useEffect(() => {
    if (next.commit) framed.current = next.commit;
  }, [next]);
  return { src: next.src, framedMode: next.framedMode, error: next.error };
}

// ── the component ────────────────────────────────────────────────────────────

export interface AppPaneProps {
  pane: PaneState;
  params: ParamsStore;
  /** `_file`. */
  file: string | null;
  /** From `useNarrowView`; `null` when the host never collapses (cards, peek). */
  narrowView?: NarrowViewState | null;
  /** Inline width from `useSplit`. */
  width?: string;
  flags?: PaneSrcFlags;
  /** Told about every `load` of the framed document, so it can re-wrap the new
   *  document's console and count reloads (T:8544, 8833 → `watchApp`). */
  watcher?: AppStateWatcher | null;
  /** PR3: the frame's `load` also re-wires the annotation listeners and repaints
   *  the pins against the fresh document. */
  onFrameLoad?: (frame: HTMLIFrameElement) => void;
  /** Handed the live element so app-state and the annotation layer can reach the
   *  document without prop-drilling a ref through the tree. */
  frameRef?: (frame: HTMLIFrameElement | null) => void;
}

export function AppPane({
  pane,
  params,
  file,
  narrowView,
  width,
  flags,
  watcher,
  onFrameLoad,
  frameRef,
}: AppPaneProps) {
  const [leftMode, setLeftMode] = useState<string | undefined>(() => params.get("leftmode"));
  useEffect(() => params.onChange((all) => setLeftMode(all.leftmode)), [params]);

  const frame = useRef<HTMLIFrameElement | null>(null);
  const { src, error: swapError } = useFramedSrc(pane.decision, leftMode, file, flags);

  const setFrame = useCallback(
    (el: HTMLIFrameElement | null) => {
      frame.current = el;
      frameRef?.(el);
    },
    [frameRef],
  );

  const onLoad = useCallback(() => {
    const el = frame.current;
    if (!el) return;
    // Same-origin, guarded inside: `watchApp` re-wraps the NEW document's
    // console (the flag lives on the document, since a same-origin navigation
    // replaces the global) and counts the load so a reload can be marked in the
    // buffer rather than clearing it.
    watcher?.watchApp();
    onFrameLoad?.(el);
  }, [watcher, onFrameLoad]);

  // Step 6 of enterNoPane: the column and its controls are simply not rendered.
  if (pane.noPane) return null;

  const modes = pane.decision?.leftModes ?? [];
  const showBar = leftBarShown(!!narrowView?.narrow, pane.noPane, modes.length);
  const message = pane.error ?? swapError;

  return (
    <div className="c-left" style={width ? { width } : undefined}>
      {/* A row ABOVE the frame, never over it: arming the annotation bar pushes
          the app down instead of covering its first 43px, and the pane's own bar
          follows the same rule (T:3894-3896). */}
      {showBar ? (
        <div className="c-leftbar">
          <LeftModePicker modes={modes} params={params} leftMode={leftMode} />
        </div>
      ) : null}
      {/* The frame's box, and the offset parent for everything that floats over
          the app (PR3's pins, highlight and note composer). */}
      <div className="c-leftview">
        {message ? (
          // Only the FRAME is swapped for the message — this box also hosts the
          // annotation layer (T:5865).
          <div className="c-msg">Could not open preview: {message}</div>
        ) : null}
        {src && !pane.error ? (
          <iframe
            ref={setFrame}
            src={src}
            title="Preview"
            onLoad={onLoad}
            // THE STAMP PR3 LOOKS FOR. In the hosted (chat_only) layout the
            // annotation layer finds its target by walking to the marked iframe;
            // here the pane marks its own, so one lookup serves both layouts and
            // neither needs `parent.document` (T:4556-4562).
            data-fused-annotate-target=""
          />
        ) : null}
      </div>
    </div>
  );
}
