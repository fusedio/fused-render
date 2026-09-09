// THE COORDINATOR (T's `renderAnn` plus everything that calls it).
//
// One hook, and it is the ONLY thing in this subsystem that knows all of the
// pieces: the store, the mode machine, the target, the layer, the six listeners
// and the composer. Everything else takes what it needs as arguments, which is
// what makes each piece testable on its own — and what keeps this file the one
// place where "which layout are we in" is answered.
//
// The integrator wires the seams this returns; `README.md` lists them.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ParamsStore } from "../params/store";
import type { CaptureOptions } from "../shots/capture";
import { chipsOf, type AnnChipItem } from "./AnnChips";
import {
  buildToolNode,
  closeComposer as closePop,
  createToolDoors,
  dismissesComposer,
  isOpen,
  openComposer as openPop,
  openEditor as openEd,
  paintTool,
  toolFromClick,
  unportalPop,
  type AnnPopoverHandlers,
  type PlacePopOptions,
} from "./AnnPopover";
import { barFit, createBarPush, paintBar } from "./AnnBar";
import { chipEditXY, clockOf, rectOf } from "./geometry";
import { createRenderQueue, createXOLayer, hideHl, injectLayer, paintPins, pinSpotOf } from "./layer";
import { createAnnMode, escapeAction, type AnnModeMachine } from "./mode";
import { applyOverview, overviewFor, type OverviewResult } from "./overview";
import { createAnnStore, isSendable, type AnnStore } from "./store";
import { createAnnTarget, type AnnTarget } from "./target";
import { wireTarget } from "./wire-target";
import type { AnnAnchor, AnnLayout, AnnMode, AnnRecorder, AnnTool, Annotation } from "./types";

export interface UseAnnotationsOptions {
  params: ParamsStore;
  /** `chat_only=1` — the pane belongs to the host. */
  hosted: boolean;
  /** `enterNoPane` has run. */
  noPane: boolean;
  /** design.md §2's prop: the host's marked iframe. Replaces T's
   *  `window.parent.document.querySelector`, so nothing here reaches into a
   *  parent document. Omitted in the split layout, where the pane marks its own
   *  frame and this document's own `querySelector` finds it. */
  annotateTarget?: () => HTMLIFrameElement | null;
  /** The parent document for the cross-origin overlay, which is the ONE thing
   *  that genuinely has to live over there (the frame's box is in the host's
   *  layout). Same origin: it marked the frame for us. Omitted → no XO overlay,
   *  which is the honest degradation. */
  parentDocument?: () => Document | null;
  /** Where the split layout's parked composer lives when it has no point to aim
   *  at (T:7291 — the chat column). */
  composerHome?: () => Element | null;
  /** T:7720 — `activeRun || !sending`. */
  canSend(): boolean;
  /** T:8487 `annAutoSubmit`. */
  autoSubmit(): void;
  /** T:6867 — the nav lock's effect: `.chat-root.annlock`, `#back.disabled`,
   *  and the composer's block state. */
  onLock?(locked: boolean): void;
  /** T:8447's strip effect: the Comment, Annotate and Screenshot seats are
   *  HIDDEN, not disabled, when there is nothing to annotate. */
  onCapableChange?(capable: boolean): void;
  /** T:7670 — the ONE tab-share prompt an armed cross-origin target needs, and
   *  only when the native screen shot is unavailable. */
  onXOArm?(): void;
  /** The voice recorder (`ann/rec*`). */
  recorder?: () => AnnRecorder | null;
  /**
   * T:7952 / 7972 — WHO WRITES THE MARK while a walkthrough owns the click.
   *
   * `ann/rec.ts` mints the id and the `t` stamp the transcript is matched
   * against (`assignWords` keys on both), so where a recorder is wired the note
   * must be ITS write and not ours: otherwise `annRecAssign` looks up ids that
   * never existed and a spoken walkthrough lands as N wordless notes. Both
   * return the id they wrote — `null` means "not mine" and the store write
   * below stands in, which is what keeps this hook whole with no recorder at
   * all.
   */
  recMark?: (anchor: AnnAnchor) => string | null;
  recMarkPoint?: (
    clientX: number,
    clientY: number,
    win: Window | null,
    nearPath?: string,
  ) => string | null;
  /** T:15959 — the shot viewer claims Escape first. */
  viewerOpen?(): boolean;
  /** Injected in tests. */
  now?(): number;
  raf?(cb: () => void): void;
  document?: Document;
}

export interface AnnotationsApi {
  annotations: readonly Annotation[];
  mode: AnnMode;
  tool: AnnTool;
  /** T:6864 `annNavLocked`. */
  locked: boolean;
  /** T:6122 `annCapable` — whether the seats show at all. */
  capable: boolean;
  layout: AnnLayout;
  /** The chips for the attachment tray's `children`. */
  chips: AnnChipItem[];

  /** The strip's Comment seat: arm from rest, ✓ Done while armed, inert while a
   *  walkthrough records (T:7695). */
  onCommentSeat(): void;
  arm(): void;
  done(): Promise<void>;
  discard(): void;
  setTool(t: AnnTool): void;
  editNote(note: Annotation): void;
  removeNote(note: Annotation): void;

  /** T:15982 — bound on BOTH documents; the frame side is re-attached per load. */
  onEscape(e: KeyboardEvent): void;
  /** `useNarrowView`'s `onArriveChat`. */
  arriveNarrowChat(): void;
  /** `useNarrowView`'s `onRemeasure` and `useSplit`'s `onDragTick`. */
  remeasure(): void;
  /** `AppPane`'s `onFrameLoad`. */
  onFrameLoad(frame: HTMLIFrameElement): void;
  /** `AppPane`'s `frameRef`, split layout. */
  bindStage(stage: HTMLElement | null): void;
  /** `AnnPins`' `onBind`. */
  bindPins(bind: { pins: HTMLElement; hl: HTMLElement; stage: HTMLElement } | null): void;
  /** `AnnBar`'s `barRef`. */
  bindBar(bar: HTMLElement | null): void;
  /** `AnnPopover`'s `popRef`. */
  bindPop(pop: HTMLElement | null): void;
  /** `AnnPopover`'s three handlers. They belong to the coordinator and not to
   *  the integrator because all three read the DRAFT — which anchor this card is
   *  about, or which note it is editing — and that is this hook's own state
   *  (T:7416 commit, T:7443 close, T:7409 delete). */
  popHandlers: AnnPopoverHandlers;

  /** `enterNoPane` steps 1a / 1b / 2 / 5. */
  clearAnnotations(): void;
  render(): void;
  setMode(on: boolean): void;
  rescueComposer(): void;

  /** The send path: stamp the letters, take the ONE badged picture, fold the
   *  result back into the notes. Returns the notes to put on the wire. */
  overviewForSend(opts?: CaptureOptions): Promise<{
    notes: Annotation[];
    overview: OverviewResult | null;
  }>;
  markSent(notes: readonly Annotation[]): void;
  unmarkSent(notes: readonly Annotation[]): void;
  /** T:10608 — a run that carried notes finished cleanly. */
  resolveSent(): void;

  /** Escape hatches for the recorder module (`ann/rec*`). */
  store: AnnStore;
  machine: AnnModeMachine;
  target: AnnTarget;
  /** T:7952 / 7972 — the store's own mark writers, the fallback for a click
   *  with no recorder behind it. UNSTAMPED: `t` is the recording clock's, and
   *  where a recording owns the click it owns the write too (`recMark`). */
  mark(anchor: AnnAnchor): Annotation;
  markPoint(clientX: number, clientY: number, win: Window | null, nearPath?: string): Annotation;
  /** The live clock text the bar mirrors (`m:ss · N`). */
  setClock(text: string): void;
}

export function useAnnotations(opts: UseAnnotationsOptions): AnnotationsApi {
  // A DOCUMENT WE CANNOT BUILD NODES IN IS NO DOCUMENT. The bar, the card and
  // the picker are imperative nodes and every one of them starts with a
  // `createElement`; a host that has a `document` global without one (a
  // renderer with no DOM, a server pass) is the "no layer" case, which every
  // path below already answers for — and answering it HERE is what keeps that
  // answer in one place instead of nine null checks.
  const hostDoc = opts.document ?? (typeof document !== "undefined" ? document : null);
  const ownDoc = hostDoc && typeof hostDoc.createElement === "function" ? hostDoc : null;
  // MEMOIZED, and that is load-bearing rather than tidy: `now` is a dependency
  // of the store below, and `() => Date.now()` is a NEW function on every render
  // — so the store was rebuilt per render, re-hydrating from the `annotations`
  // param and undoing every write the param had not coalesced yet. A cleared
  // list came straight back.
  const nowOpt = opts.now;
  const now = useMemo(() => nowOpt ?? (() => Date.now()), [nowOpt]);

  // ── the pieces, built once ───────────────────────────────────────────────
  const store = useMemo(
    () => createAnnStore({ params: opts.params, hosted: opts.hosted, now }),
    // Rebuilding the store would drop the round and the list; the params store
    // and the layout are fixed for the life of a mount.
    [opts.params, opts.hosted, now],
  );

  const [list, setList] = useState<readonly Annotation[]>(() => store.list());
  const [mode, setMode] = useState<AnnMode>("off");
  const [tool, setToolState] = useState<AnnTool>("element");
  const [locked, setLocked] = useState(false);
  const [capable, setCapable] = useState(true);
  const clock = useRef("");

  // The live nodes. Refs rather than state: they are written by DOM callbacks
  // and read by the paint, and a re-render for each would be a render per frame
  // of a scroll.
  const layerRef = useRef<{
    pins: Element | null;
    hl: HTMLElement | null;
    bar: HTMLElement | null;
    stage: Element | null;
    root: ShadowRoot | null;
  }>({ pins: null, hl: null, bar: null, stage: null, root: null });
  const splitPins = useRef<{ pins: HTMLElement; hl: HTMLElement; stage: HTMLElement } | null>(null);
  const splitBar = useRef<HTMLElement | null>(null);
  const popRef = useRef<HTMLElement | null>(null);
  const draft = useRef<AnnAnchor | null>(null);
  const editing = useRef<string | null>(null);
  const liveOpts = useRef(opts);
  liveOpts.current = opts;
  const machineRef = useRef<AnnModeMachine | null>(null);
  const targetRef = useRef<AnnTarget | null>(null);
  const barPush = useRef<((doc: Document | null) => void) | null>(null);
  if (!barPush.current) barPush.current = createBarPush();

  // The picker: ONE imperative node for the life of the mount, because it rides
  // the bar into the app's document (T:6860).
  const toolNode = useRef<HTMLElement | null>(null);
  if (!toolNode.current && ownDoc) toolNode.current = buildToolNode(ownDoc);
  const onPickerClick = useRef((e: Event) => {
    const t = toolFromClick(e);
    if (t) setToolNow(t);
  });
  const doors = useRef(createToolDoors(() => toolNode.current));

  const setToolNow = useCallback((t: AnnTool) => {
    setToolState(t);
    if (toolNode.current) paintTool(toolNode.current, t);
  }, []);

  // ── the composer's placement contract ────────────────────────────────────
  const placeOpts = useCallback((): PlacePopOptions | null => {
    const pop = popRef.current;
    if (!pop || !ownDoc) return null;
    return {
      pop,
      stage: layerRef.current.stage as { clientWidth: number; clientHeight: number } | null,
      root: layerRef.current.root,
      hosted: liveOpts.current.hosted,
      ownDoc,
      home: liveOpts.current.composerHome ?? (() => ownDoc.body),
      // THE SPLIT LAYOUT'S STAGE NODE, and it is what makes the placement
      // coordinates mean what they say. `placePop`'s left/top are FRAMED
      // VIEWPORT pixels; parked in the chat column the card's containing block
      // is `.c-chat` (`position: relative`), so a note on an element near the
      // top of the pane opened a card over the strip's own buttons instead of
      // beside the element (QA round 2, item 3). Hosted and XO solve this by
      // portaling into the layer; split has no shadow root to portal into, so
      // the card moves into the pane's view box — the very element `AnnPins`
      // puts the pins and the ring in, i.e. the box those coordinates are
      // measured against.
      stageEl: () => (liveOpts.current.hosted ? null : (splitPins.current?.stage ?? null)),
    };
  }, [ownDoc]);

  const composerOpen = useCallback(() => isOpen(popRef.current), []);
  const closeComposer = useCallback(() => {
    const o = placeOpts();
    draft.current = null;
    editing.current = null;
    if (o) closePop(o);
  }, [placeOpts]);

  // ── the paint ────────────────────────────────────────────────────────────
  const queue = useRef<{ queue: () => void; dispose: () => void } | null>(null);

  const render = useCallback(() => {
    const target = targetRef.current;
    const machine = machineRef.current;
    if (!target || !machine || !ownDoc) return;
    // T:6874 — `renderAnn` ends in `annNavLock()`. Asserted FIRST here so the
    // `noPane` early return below cannot skip it: the lock is derived, and a
    // lock left on is a chat the reader has no way to leave.
    machine.relock();
    // No pane and not hosted: the notes point at a document this target does not
    // have, so there are no pins and no ring — but the CHIPS are still the
    // payload of a message this chat can send, and the attached pictures have
    // nothing to do with a pane at all (T:6875). The chip row is React's, so it
    // needs nothing here beyond the list it already subscribes to.
    if (liveOpts.current.noPane && !liveOpts.current.hosted) {
      layerRef.current = { pins: null, hl: null, bar: null, stage: null, root: null };
      barPush.current?.(null);
      return;
    }
    target.sync();
    const bar = layerRef.current.bar ?? splitBar.current;
    if (bar) {
      // A bar in ANOTHER document pushes that document down while it shows; a
      // bar that went away hands the margin back (Bugbot, PR #1008).
      const hostedDoc =
        bar.ownerDocument !== ownDoc && !target.xo() ? bar.ownerDocument : null;
      const show = machine.mode() === "comment" || machine.mode() === "recording";
      barPush.current?.(show ? hostedDoc : null);
      paintBar(bar, {
        mode: machine.mode(),
        clock: clock.current,
        picker: target.xo() ? null : toolNode.current,
        onPickerClick: onPickerClick.current,
        ownDoc,
      });
    } else {
      barPush.current?.(null);
    }
    const doc = target.doc();
    paintPins({
      pins: layerRef.current.pins,
      stage: layerRef.current.stage as { clientWidth: number; clientHeight: number } | null,
      armed: machine.armed(),
      list: store.list(),
      roundStart: store.roundStart(),
      doc,
      xo: target.xo(),
      resolve: (c, d) => store.resolve(c, d),
      onPinClick: (c, left, top) => openEditorAt(c, left, top),
    });
    // THE LOAN CHECK (T:6132): the composer, while portaled, is a node we LENT
    // to that document — and this is the one place that can notice the loan has
    // gone bad. A live reload replaces the document, the shell's mode switch
    // moves the mark, an unmarked pane leaves no layer at all. In every one of
    // those the root we handed the card to is not the root we just resolved.
    // Close it, which also brings it home: a close DROPS the draft, and that is
    // the honest outcome rather than a bug — the anchor the half-typed note
    // pointed at went with the document.
    const pop = popRef.current;
    if (pop && pop.ownerDocument !== ownDoc && pop.getRootNode() !== layerRef.current.root) {
      closeComposer();
    }
  }, [ownDoc, store, closeComposer]);
  const renderRef = useRef(render);
  renderRef.current = render;
  if (!queue.current) {
    queue.current = createRenderQueue(() => renderRef.current(), opts.raf);
  }

  // ── the composer's two doors ─────────────────────────────────────────────
  const openComposerAt = useCallback(
    (x: number, y: number, anchor: AnnAnchor) => {
      const o = placeOpts();
      if (!o) return;
      draft.current = anchor;
      editing.current = null;
      openPop(anchor, x, y, o);
    },
    [placeOpts],
  );

  const openEditorAt = useCallback(
    (note: Annotation, x: number | null, y: number | null) => {
      const o = placeOpts();
      if (!o) return;
      // TOGGLE: clicking the same note's marker while its editor is open closes
      // it (T:7367).
      if (editing.current === note.id && composerOpen()) {
        closeComposer();
        return;
      }
      draft.current = null;
      editing.current = note.id;
      openEd(note, x, y, o);
    },
    [placeOpts, composerOpen, closeComposer],
  );

  /** T:7416 `annCommit` — the ONE commit path, the Enter key's. */
  const commit = useCallback(
    (text: string) => {
      if (text && draft.current) store.add({ content: text, ...draft.current });
      else if (text && editing.current) store.edit(editing.current, text);
      closeComposer();
    },
    [store, closeComposer],
  );

  const commitRef = useRef(commit);
  commitRef.current = commit;
  const closeRef = useRef(closeComposer);
  closeRef.current = closeComposer;

  const commitDraft = useCallback(() => {
    const pop = popRef.current;
    const ta = pop && (pop.querySelector("textarea") as HTMLTextAreaElement | null);
    const text = ta ? ta.value.trim() : "";
    if (text) commit(text);
  }, [commit]);

  /**
   * The card's three buttons, as ONE stable object: the node is built once per
   * mount (`AnnPopover`) and reads its handlers through the component's own ref,
   * so these must not be rebuilt per render — and each of the three is a read of
   * a ref, so none of them ever needs to be.
   */
  const popHandlers = useRef<AnnPopoverHandlers>({
    commit: (text) => commitRef.current(text),
    close: () => closeRef.current(),
    del: () => {
      const id = editing.current;
      closeRef.current();
      if (id) store.remove(id);
    },
  });

  // ── the mode machine ─────────────────────────────────────────────────────
  if (!machineRef.current) {
    machineRef.current = createAnnMode({
      store,
      capable: () => targetRef.current?.capable() ?? false,
      recorder: () => liveOpts.current.recorder?.() ?? null,
      render: () => renderRef.current(),
      onLock: (l) => {
        setLocked(l);
        liveOpts.current.onLock?.(l);
      },
      onToolVisible: (show) => (show ? doors.current.show() : doors.current.hide()),
      composerOpen: () => isOpen(popRef.current),
      composerText: () => {
        const ta = popRef.current?.querySelector("textarea") as HTMLTextAreaElement | null;
        return ta ? ta.value : "";
      },
      commitDraft: () => commitDraft(),
      closeComposer: () => closeComposer(),
      hideHl: () => hideHl(layerRef.current.hl),
      autoSubmit: () => liveOpts.current.autoSubmit(),
      canSend: () => liveOpts.current.canSend(),
      xo: () => targetRef.current?.xo() ?? false,
      onXOArm: () => liveOpts.current.onXOArm?.(),
      now,
    });
  }
  const machine = machineRef.current;

  // ── the target ───────────────────────────────────────────────────────────
  if (!targetRef.current) {
    // ONE overlay for the life of the mount, its frame and its host document
    // re-read on every resolve: the mark moves between the frames the shell
    // keeps mounted, and rebuilding the layer per resolve would drop its host
    // and flash a new overlay on every poll.
    const xo = createXOLayer({
      frame: () => targetRef.current?.frame() ?? null,
      parentDoc: () => liveOpts.current.parentDocument?.() ?? null,
      bar: barHandlers(),
      onPoint: (x, y) => onXOPoint(x, y),
      armed: () => machineRef.current?.armed() ?? false,
    });
    targetRef.current = createAnnTarget({
      hosted: opts.hosted,
      noPane: () => liveOpts.current.noPane,
      markedFrame: opts.annotateTarget,
      queueRender: () => queue.current?.queue(),
      render: () => renderRef.current(),
      wireDoc: (doc) =>
        wireTarget(doc, {
          armed: () => machineRef.current?.armed() ?? false,
          recording: () => liveOpts.current.recorder?.()?.recording() ?? false,
          // THE SETTLE IS NOT COMMENT MODE. `armed()` stays true through
          // Stopping…/Transcribing… (the transcription is this chat's) while
          // the recorder's own flag is already down, so without this the
          // in-frame handlers read the settle as a typed round: the click was
          // swallowed and a composer opened under a hidden bar (Bugbot,
          // PR #1074).
          settling: () => {
            const m = machineRef.current?.mode();
            return m === "settling" || m === "transcribing";
          },
          tool: () => toolRef.current,
          composerOpen: () => isOpen(popRef.current),
          hl: () => layerRef.current.hl,
          onEscape: (e) => onEscapeRef.current(e),
          closeComposer: () => closeComposer(),
          openComposer: (x, y, anchor) => openComposerAt(x, y, anchor),
          markPoint: (cx, cy, win, nearPath) => {
            markPointRef.current(cx, cy, win, nearPath);
          },
          mark: (anchor) => {
            markRef.current(anchor);
          },
          queueRender: () => queue.current?.queue(),
        }),
      onArrive: () => machineRef.current?.bootFromParam(),
      onLeave: () => machineRef.current?.targetGone(),
      onCapableChange: (c) => {
        setCapable(c);
        liveOpts.current.onCapableChange?.(c);
      },
      // The overlay's host is our node in the PARENT's document, and XO IS the
      // hosted layout — so `removeInjectedLayer` (the one teardown that runs
      // for it) has to be able to take this one too. Nothing else can: the XO
      // branch of `sync` leaves the target's document and `layerDoc` both null.
      removeXOLayer: () => xo.remove(),
      resolveLayer: (doc, isXo) => {
        if (!isXo) xo.remove();
        const bind = isXo
          ? xo.resolve()
          : liveOpts.current.hosted
            ? injectLayer(doc, barHandlers())
            : null;
        if (bind) {
          layerRef.current = { ...bind, stage: bind.stage };
          return;
        }
        // The split layout's boxes are React's (`AnnPins`), and its bar is the
        // row in this document — no shadow root, and nothing to portal INTO,
        // which is also what makes the portal unreachable there without a second
        // flag (T:6041).
        const sp = splitPins.current;
        layerRef.current = {
          pins: sp ? sp.pins : null,
          hl: sp ? sp.hl : null,
          bar: splitBar.current,
          stage: sp ? sp.stage : null,
          root: null,
        };
      },
      document: ownDoc ?? undefined,
    });
  }
  const target = targetRef.current;

  const toolRef = useRef<AnnTool>(tool);
  toolRef.current = tool;

  // ── the recorder's mark writers ──────────────────────────────────────────
  const markRef = useRef<(anchor: AnnAnchor) => void>(() => {});
  const markPointRef = useRef<
    (cx: number, cy: number, win: Window | null, nearPath?: string) => void
  >(() => {});
  // NO `t` ON EITHER FALLBACK, and that is the honest answer rather than a
  // missing feature. `t` means "seconds into the recording", and these two
  // writers are exactly the path taken when there is no recorder to have one:
  // `wire-target` asks `recMark`/`recMarkPoint` first and only falls through
  // when the recorder declines. They used to stamp `stampOf(atMs - recStart)`
  // against a `recStart` NOTHING ever wrote, i.e. a raw `performance.now()` —
  // an absolute 1.8e9 that `assignWords` would then sort the transcript on. The
  // stamp belongs to whoever owns the clock, and where no clock is running the
  // answer is no stamp (`setRecStart` went with it).
  const mark = useCallback(
    (anchor: AnnAnchor) => store.add({ content: "", ...anchor }),
    [store],
  );
  const markPoint = useCallback(
    (cx: number, cy: number, win: Window | null, nearPath?: string) => {
      const anchor: AnnAnchor = {
        kind: "point",
        x: Math.round(cx + (win ? win.scrollX : 0)),
        y: Math.round(cy + (win ? win.scrollY : 0)),
      };
      if (nearPath) anchor.nearPath = nearPath;
      return store.add({ content: "", ...anchor });
    },
    [store],
  );
  /** A mark writer that DECLINED while its recorder calls itself recording is
   *  the START WINDOW (`state === "starting"` — the mic prompt): the click
   *  belongs to a walkthrough that has no clock to stamp it against yet.
   *  Dropped rather than written as a typed note, because a wordless typed note
   *  under a bar that says "Voice annotation" is the one reading that is wrong
   *  whichever way the start ends (Bugbot, PR #1074). Only where there IS a
   *  writer: a caller with no `recMark` of its own still gets the fallback
   *  above, which is the whole reason it exists. */
  const startWindow = () => !!liveOpts.current.recorder?.()?.recording();
  markRef.current = (anchor) => {
    const write = liveOpts.current.recMark;
    if (write) {
      if (write(anchor)) return;
      if (startWindow()) return;
    }
    mark(anchor);
  };
  markPointRef.current = (cx, cy, win, nearPath) => {
    const write = liveOpts.current.recMarkPoint;
    if (write) {
      if (write(cx, cy, win, nearPath)) return;
      if (startWindow()) return;
    }
    markPoint(cx, cy, win, nearPath);
  };

  function onXOPoint(x: number, y: number): void {
    const r = liveOpts.current.recorder?.();
    if (r && r.recording()) {
      markPointRef.current(x, y, null);
      return;
    }
    // Overlay-relative from birth, and `shot: null` stays null for ever: there
    // is nothing we are allowed to rasterise over there (T:6329).
    openComposerAt(x, y, { kind: "point", x, y, shot: null });
  }

  function barHandlers() {
    return {
      onDone: () => void machineRef.current?.done(),
      onStop: () => liveOpts.current.recorder?.()?.end(),
      onDiscard: () => machineRef.current?.discard(),
      onResize: (b: HTMLElement) => {
        if (b === (layerRef.current.bar ?? splitBar.current)) barFit(b);
      },
    };
  }

  // ── Escape ───────────────────────────────────────────────────────────────
  const onEscapeRef = useRef<(e: KeyboardEvent) => void>(() => {});
  const onEscape = useCallback(
    (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const act = escapeAction(
        liveOpts.current.viewerOpen?.() ?? false,
        isOpen(popRef.current),
        machineRef.current?.armed() ?? false,
      );
      if (!act) return;
      // A PRESS WE CANNOT ANSWER IS NOT OURS TO CLAIM. The viewer is the host's
      // to close, and this handler is bound on BOTH documents: on the chat's the
      // chat's own listener has already returned early for an open viewer, and
      // in the FRAMED document there is nothing here that can close it — so
      // `preventDefault` would only eat the key and leave the viewer open.
      if (act === "close-viewer") return;
      e.preventDefault();
      if (act === "close-composer") closeComposer();
      else if (act === "exit-annotate") machineRef.current?.escape();
    },
    [closeComposer],
  );
  onEscapeRef.current = onEscape;

  // ── wiring ───────────────────────────────────────────────────────────────
  useEffect(
    () =>
      store.subscribe((next) => {
        setList(next);
        // AND REPAINT. The chips are React's and update themselves from this
        // very state; the PINS are not — they are `paintPins`' imperative nodes
        // in whichever document the layer stands in, and nothing else in this
        // subsystem watches the list. So a note committed by the composer got
        // its chip and no pin until something unrelated (a resize, a scroll, a
        // mode change) happened to repaint. T ends `annSave` in a render for
        // exactly this reason; the queue is what makes a merge of N notes one
        // frame instead of N.
        queue.current?.queue();
      }),
    [store],
  );
  useEffect(() => machine.subscribe((m) => setMode(m)), [machine]);

  useEffect(() => {
    // The boot arm and the target loop, in T's order: the boot default reads
    // `annmode` (OFF unless it says exactly "1"), then the poll starts asking.
    machine.bootFromParam();
    const stop = target.start();
    return () => {
      stop();
      // A frame queued by a scroll or a mutation would otherwise fire after the
      // unmount and paint into detached nodes.
      queue.current?.dispose();
    };
  }, [machine, target]);

  useEffect(() => {
    // THIS document's half of the outside-click dismissal (T:7395). The other
    // half hangs off the target's document, because a click inside a frame never
    // bubbles out of it — each document dismisses what the other cannot see.
    const doc = ownDoc;
    if (!doc) return;
    const onDown = (e: Event) => {
      const pop = popRef.current;
      if (pop && dismissesComposer(pop, e.target)) closeComposer();
    };
    doc.addEventListener("mousedown", onDown);
    return () => doc.removeEventListener("mousedown", onDown);
  }, [ownDoc, closeComposer]);

  useEffect(() => {
    // A theme flip repaints the bar: the tokens are COPIED onto a bar standing
    // in another document, not live (T:6797).
    const doc = ownDoc;
    const view = doc && doc.defaultView;
    if (!doc || !view || !view.MutationObserver) return;
    const mo = new view.MutationObserver(() => renderRef.current());
    mo.observe(doc.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, [ownDoc]);

  useEffect(() => {
    // T:8849 — a window resize re-measures every pin.
    const view = ownDoc && ownDoc.defaultView;
    if (!view) return;
    const onResize = () => renderRef.current();
    view.addEventListener("resize", onResize);
    return () => view.removeEventListener("resize", onResize);
  }, [ownDoc]);

  useEffect(() => {
    // T:8748 — a reload or close mid-walkthrough ASKS first (Akshil, 2026-09-07,
    // option 1): the recording cannot survive the page, so the one thing this
    // document can do for it is make leaving deliberate. EVERY layout (Bugbot,
    // PR #1046) — the full-page and split chats record too.
    const view = ownDoc && ownDoc.defaultView;
    if (!view) return;
    const onBeforeUnload = (e: Event) => {
      if (!liveOpts.current.recorder?.()?.recording()) return;
      e.preventDefault();
      (e as BeforeUnloadEvent).returnValue = "A walkthrough is recording — leave and stop it?";
    };
    view.addEventListener("beforeunload", onBeforeUnload);
    return () => view.removeEventListener("beforeunload", onBeforeUnload);
  }, [ownDoc]);

  useEffect(() => {
    // T:8776 — the HOSTED teardown. The layer is OUR node in THEIR document, and
    // the host tears this component down without warning; nothing survives us to
    // clean it up, so the pins were left painted over the content pane for as
    // long as the user stayed in the other mode. DOM removal only.
    if (!opts.hosted) return;
    const view = ownDoc && ownDoc.defaultView;
    if (!view) return;
    const teardown = () => {
      machineRef.current?.setPhase(null);
      const r = liveOpts.current.recorder?.();
      // `stop()` KEEPS the file: a teardown nobody asked for must not throw a
      // walkthrough away.
      if (r && r.recording()) r.end();
      targetRef.current?.disconnectObserver();
      targetRef.current?.removeInjectedLayer();
      // AND THE MARGIN BACK. `createBarPush` puts an `!important` 43px
      // `margin-top` on the app's root while the bar shows; the bar goes with
      // the layer above, and without this hand-back the content pane was left
      // pushed down by a bar that is not there for as long as the reader stayed
      // in that mode (measured in the browser, PR3 round 2).
      barPush.current?.(null);
      const pop = popRef.current;
      if (pop && ownDoc) unportalPop(pop, ownDoc, liveOpts.current.composerHome ?? (() => ownDoc.body));
      targetRef.current?.releaseGuards();
    };
    view.addEventListener("pagehide", teardown);
    return () => {
      view.removeEventListener("pagehide", teardown);
      // A React unmount is the same event as far as the host's document is
      // concerned: the guards are on objects that outlive us, and to the next
      // instance they would read as "already adopted".
      teardown();
    };
  }, [opts.hosted, ownDoc]);

  // ── the API ──────────────────────────────────────────────────────────────
  const chips = useMemo(() => chipsOf(list), [list]);

  const editNote = useCallback(
    (note: Annotation) => {
      const t = targetRef.current;
      const doc = t?.doc() ?? null;
      const spot = t ? pinSpotOf(note, doc, t.xo(), (c, d) => store.resolve(c, d)) : null;
      const el = !spot && doc ? store.resolve(note, doc) : null;
      const at = chipEditXY(
        spot,
        el ? rectOf(el) : null,
        layerRef.current.stage as { clientWidth: number; clientHeight: number } | null,
        liveOpts.current.hosted,
      );
      openEditorAt(note, at.x, at.y);
    },
    [store, openEditorAt],
  );

  const removeNote = useCallback(
    (note: Annotation) => {
      if (editing.current === note.id) closeComposer();
      store.remove(note.id);
    },
    [store, closeComposer],
  );

  return {
    annotations: list,
    mode,
    tool,
    locked,
    capable,
    layout: target.layout(),
    chips,

    onCommentSeat() {
      // WHILE A WALKTHROUGH OWNS THE MODE THE SEAT DOES NOTHING (2026-09-06):
      // the stop is the bar's ■, not a neighbouring seat. Through the START
      // window and the settle as well as the recording itself — the same set
      // `seatsAria` draws inert, so what the seat SAYS and what it DOES cannot
      // disagree, and a click reaching this from a stale render mid-settle
      // cannot send the marks the transcript is still on its way to fill
      // (Bugbot, PR #1074).
      const m = machine.mode();
      if (m !== "off" && m !== "comment") return;
      if (machine.armed()) void machine.done();
      else machine.set(true);
    },
    arm: () => machine.set(true),
    done: () => machine.done(),
    discard: () => machine.discard(),
    setTool: setToolNow,
    editNote,
    removeNote,

    onEscape,
    arriveNarrowChat: () => machine.arriveNarrowChat(),
    remeasure: () => renderRef.current(),
    onFrameLoad: () => {
      // The frame's own `load` is what `target.watch` already listens for; this
      // seam exists for the host that owns the element and would rather tell us
      // than have us bind a second listener to it.
      target.sync();
      renderRef.current();
    },
    bindStage: (stage) => {
      // The pane's view box, for the layouts whose stage is a node of THIS
      // document. Nothing to hold: `AnnPins` binds the boxes inside it, and the
      // stage comes back with them — this seam only says "the pane moved".
      if (stage) renderRef.current();
    },
    bindPins: (bind) => {
      splitPins.current = bind;
      renderRef.current();
    },
    bindBar: (bar) => {
      splitBar.current = bar;
      renderRef.current();
    },
    bindPop: (pop) => {
      popRef.current = pop;
    },
    popHandlers: popHandlers.current,

    clearAnnotations: () => store.clear(),
    render: () => renderRef.current(),
    setMode: (on) => machine.set(on),
    rescueComposer: () => closeComposer(),

    async overviewForSend(captureOpts) {
      const pending = store.pending().filter(isSendable);
      if (!pending.length) return { notes: [], overview: null };
      // The letters FIRST, because the badge and the wire's `label` are the same
      // string and the picture is drawn from it (T:16053).
      const stamped = store.stampLabels(pending);
      const t = targetRef.current;
      const doc = t?.doc() ?? null;
      const overview = await overviewFor(
        t?.frame() ?? null,
        stamped,
        {
          doc,
          xo: t?.xo() ?? false,
          stage: layerRef.current.stage as { clientWidth: number; clientHeight: number } | null,
          resolve: (c, d) => store.resolve(c, d),
        },
        captureOpts,
      );
      const notes = applyOverview(stamped, overview ? overview.marks : null);
      store.merge(notes);
      return { notes, overview };
    },
    markSent: (notes) => store.markSent(notes),
    unmarkSent: (notes) => store.unmarkSent(notes),
    resolveSent: () => store.resolveSent(),

    store,
    machine,
    target,
    mark,
    markPoint,
    setClock: (text) => {
      clock.current = text;
      const bar = layerRef.current.bar ?? splitBar.current;
      if (bar) paintBar(bar, {
        mode: machine.mode(),
        clock: text,
        picker: target.xo() ? null : toolNode.current,
        onPickerClick: onPickerClick.current,
        ownDoc: ownDoc ?? bar.ownerDocument,
      });
    },
  };
}

/** T:6801's clock text — `m:ss · N`, one writer for the strip's label and the
 *  bar's `.clk` so the two faces of one fact cannot disagree. */
export function recClockText(elapsedMs: number, marks: number): string {
  return clockOf(elapsedMs / 1000) + (marks ? " · " + marks : "");
}

/** T:6816 `annSeatsAria` — the strip's inert seats, spoken: `aria-disabled`
 *  follows the same two state classes the stylesheet dims by (Akshil,
 *  2026-09-06). Exported for the integrator, since the seats are `ui/AnnStrip`'s
 *  and this hook does not own them. */
export function seatsAria(mode: AnnMode): {
  comment: boolean;
  annotate: boolean;
  screenshot: boolean;
} {
  const recording = mode === "recording";
  const armed = mode !== "off";
  // THE WALKTHROUGH OWNS THE SEAT UNTIL ITS WORDS LAND (Bugbot, PR #1074).
  // `recording` alone was the whole test, so through Stopping…/Transcribing…
  // the seat came back to life wearing the `.on` ✓ Done face — and a click
  // there ran `done()`: it auto-submitted the walkthrough's wordless stamped
  // marks and disarmed the mode mid-transcription, so the transcript landed on
  // notes that had already been sent empty. Every state but `off` and `comment`
  // is the recorder's, start window and settle alike, and the seat is inert for
  // all of them (`COMMENT_SEAT_WHILE_SETTLING` already NAMES it that way — this
  // is the other half of the same fact).
  const owned = armed && mode !== "comment";
  return { comment: owned, annotate: armed && !recording, screenshot: armed };
}
