// WHICH DOCUMENT A NOTE POINTS AT (T:6026-6135, 8447-8530, 8776-8807).
//
// Bindings rather than constants, and that IS the retargeting mechanism:
// everything downstream (the pins, the ring, the capture, the anchor
// extraction) asks this module and never asks which layout it is in.
//
//   split   the pane is OURS — `pane/AppPane` stamps its own iframe with
//           `data-fused-annotate-target`, so one lookup serves both layouts and
//           neither needs `parent.document` (T:4556).
//   hosted  `chat_only=1`: the target is the host's iframe, which appears after
//           the shell's first paint, is REPLACED when the reader switches the
//           pane's mode (the mark MOVES), and disappears when the pane shows
//           something unannotatable. None of that reaches this component as an
//           event and the one mechanism that would carry it — postMessage — is
//           deliberately absent (D3/D4). So we ASK: `focus` plus a slow poll.
//   xo      a marked frame whose `contentDocument` THROWS. Point notes only.
//   none    no target at all; only screenshot chips render (D239).
//
// Every same-origin read is inside a try/catch and every failure is the SAME
// answer, null: a parent that is not there, a cross-origin one, a host that
// marks nothing, a frame mid-navigation. Null means "this is a chat"; it never
// means an error (T:6111).
import { removeLayer } from "./layer";
import { ANN_TARGET_MARK, ANN_TARGET_POLL_MS, ANN_LAYER_MARK, type AnnLayout } from "./types";

/** The iframe expando `__fusedAnnWatched` and the document expando
 *  `__fusedAnnWired` (T:8496/8524). Declared as one interface because both are
 *  OURS on objects the HOST owns and outlives us — which is exactly why
 *  `releaseGuards` exists. */
interface AnnGuards {
  __fusedAnnWatched?: boolean;
  __fusedAnnWired?: boolean;
}

export interface AnnTargetOptions {
  /** `chat_only=1`. */
  hosted: boolean;
  /** `enterNoPane` has run — the split layout's own answer to "is there a
   *  pane". */
  noPane: () => boolean;
  /**
   * The marked frame. Hosted this is the host's `annotateTarget` prop
   * (design.md §2 — it replaces T's `window.parent.document.querySelector`, so
   * the port never reaches into a parent document); split it defaults to this
   * document's own stamped iframe.
   */
  markedFrame?: () => HTMLIFrameElement | null;
  /** rAF-coalesced repaint (T:6057 `annQueueRender`). */
  queueRender: () => void;
  /** Immediate repaint (T:6875 `renderAnn`). */
  render: () => void;
  /**
   * Wire the six listeners into a fresh document (`wire-target.ts`), and hand
   * the TEARDOWN back.
   *
   * `wireTarget` owns the `__fusedAnnWired` guard — it sets it, it is idempotent
   * against it, and its teardown clears it. This module used to set the guard
   * itself one line before calling in, which made every call a no-op and left
   * the framed document with no listeners at all; and it used to throw the
   * teardown away, which stacked a second set of capture-phase click swallowers
   * on the next mount. So: never write the guard here, and keep every `off()`.
   */
  wireDoc: (doc: Document, frame: HTMLIFrameElement) => (() => void) | void;
  /** T:8471 — a target that ARRIVED re-applies the boot default, because the
   *  boot may have run before the host stamped anything. */
  onArrive?: () => void;
  /** T:8479 — a target that WENT disarms (without writing the param). */
  onLeave?: () => void;
  /** T:8447 — the poll's own effect on the strip: the switch, the mic and the
   *  screenshot buttons are HIDDEN, not disabled, when there is nothing to
   *  annotate ("absent beats dead"). */
  onCapableChange?: (capable: boolean) => void;
  /** The layer resolver (`layer.ts`). Handed the target's document, or null
   *  with `xo` true for the parent-document overlay. */
  resolveLayer?: (doc: Document | null, xo: boolean) => void;
  /**
   * `createXOLayer().remove` — the one teardown seam the injected layer's
   * removal cannot serve.
   *
   * The XO overlay's host is OUR node in the PARENT's document, and XO IS the
   * hosted layout, so the only teardown that runs for it is the hosted one,
   * which calls `removeInjectedLayer()`. That removes `[data-fused-annotate]`
   * from the TARGET's document — null in XO — and from `layerDoc`, which the XO
   * branch of `sync()` has already retargeted to null. So nothing removed the
   * overlay at all: a `position: fixed`, `z-index: 2147483646` crosshair
   * click-swallower left over the shell's iframe, calling `onPoint` into a dead
   * hook. Handed in here so the ONE teardown that runs takes BOTH hosts.
   */
  removeXOLayer?: () => void;
  document?: Document;
  win?: Window;
}

export interface AnnTarget {
  frame(): HTMLIFrameElement | null;
  /** The target's document, or null: navigating, cross-origin, or absent. */
  doc(): Document | null;
  /** T:6139 `annXO` — the marked frame's document is out of reach. */
  xo(): boolean;
  /** T:6122 `annCapable` — is there anything to annotate RIGHT NOW. */
  capable(): boolean;
  layout(): AnnLayout;
  /** T:6120 `annSyncTarget` — point the bindings at whatever is marked NOW.
   *  Returns whether the FRAME changed, which is the one thing nothing else in
   *  this component can hear. */
  sync(): boolean;
  /** T:8450 `annPollTarget`. */
  poll(): void;
  /** T:8490 `annWatchTarget` — adopt a frame: hear its future loads, and wire
   *  whatever document is in it NOW (a document already in a frame fires no
   *  event for a listener that arrives late). */
  watch(frame: HTMLIFrameElement | null): void;
  /** Installs the poll + focus (hosted) or adopts our own frame (split).
   *  Returns the teardown. */
  start(): () => void;
  /** T:8802 — a RELEASE, not tidiness: both guards live on objects the HOST
   *  owns, so to the NEXT instance they read as "already adopted" and its
   *  wiring never runs. That was the armed-but-dead switch. */
  releaseGuards(): void;
  /** T:8794 — remove the layer host from the target's document by hand, since
   *  nothing survives us to do it. */
  removeInjectedLayer(): void;
  /** T:6087's observer, exposed so the pagehide teardown can disconnect it
   *  EXPLICITLY rather than trust it to die with us: a mutation between the
   *  teardown and our destruction would re-inject the layer and re-set the very
   *  guards just cleared. */
  disconnectObserver(): void;
}

export function createAnnTarget(opts: AnnTargetOptions): AnnTarget {
  const doc0 = opts.document ?? (typeof document !== "undefined" ? document : null);
  const win0 = opts.win ?? (typeof window !== "undefined" ? window : null);

  const findMarked =
    opts.markedFrame ??
    (() => {
      if (!doc0) return null;
      const el = doc0.querySelector("[" + ANN_TARGET_MARK + "]");
      return el && el.tagName === "IFRAME" ? (el as HTMLIFrameElement) : null;
    });

  // Resolved EAGERLY, at construction, not at the first poll: the boot arm asks
  // `capable()` before anything else runs, and answering it with our own frame
  // in the hosted layout would arm a mode over a column that is about to be
  // removed (T:6026).
  let frame: HTMLIFrameElement | null = safeMarked(findMarked);
  let isXO = false;
  let observer: MutationObserver | null = null;
  let observedDoc: Document | null = null;
  let lastCapable: boolean | null = null;
  // WHAT WE PUT IN DOCUMENTS AND FRAMES THAT OUTLIVE US, so it can all come
  // back out. Keyed by the object it was installed on, because the mark moves
  // between the frames the shell keeps mounted and a reload replaces the
  // document under a frame we already adopted — "the current one" is not enough
  // to undo either.
  const wired = new Map<Document, () => void>();
  const watched = new Map<HTMLIFrameElement, () => void>();
  // The document our injected layer currently stands in, so a mark that MOVES
  // takes the overlay (and the bar's ResizeObserver) with it instead of leaving
  // one painted over a document nobody is looking at.
  let layerDoc: Document | null = null;

  function targetDoc(): Document | null {
    try {
      return frame ? frame.contentDocument : null;
    } catch {
      return null; // navigating, or a frame that turned out to be cross-origin
    }
  }

  /** T:6087 `annObserveTarget` — ONE observer, MOVED, rather than one per
   *  document left running. It has to move because the shell keeps its pane-mode
   *  iframes mounted and moves the mark between them: re-selecting a frame we
   *  already wired runs no wiring at all, so an observer left on the frame the
   *  reader just LEFT never fires for the frame they are looking at. */
  function observe(next: Document | null): void {
    if (next === observedDoc) return; // idempotent: callers pay one comparison
    if (observer) observer.disconnect();
    observer = null;
    observedDoc = next || null;
    if (!next) return;
    const view = next.defaultView;
    const Ctor = view && view.MutationObserver;
    if (!Ctor) return;
    // RECORDS, not a bare repaint, for the one mutation that is not the app
    // changing: OURS. The shadow tree the pins live in is invisible to this
    // observer, but the layer's HOST is an ordinary child of the app's body, so
    // its arrival is an ordinary childList record — and a render triggered by
    // having rendered is at best a wasted frame and at worst a loop.
    observer = new Ctor((records: MutationRecord[]) => {
      const ours = (n: Node) =>
        n.nodeType === 1 &&
        typeof (n as Element).hasAttribute === "function" &&
        (n as Element).hasAttribute(ANN_LAYER_MARK);
      for (const r of records) {
        const moved = [...Array.from(r.addedNodes), ...Array.from(r.removedNodes)];
        if (moved.length && moved.every(ours)) continue;
        opts.queueRender();
        return;
      }
    });
    observer.observe(next.body || next.documentElement, { childList: true, subtree: true });
  }

  // RE-ENTRANCY, not recursion. `sync()` adopts a frame, `watch()` wires the
  // document already in it (nothing else would — its `load` fired long ago),
  // and `onFrameLoad` both re-syncs (hosted) and ends in a full `render()`,
  // which itself calls `sync()`. Bounded by `__fusedAnnWatched`, but one
  // adoption paid two `resolveLayer` round trips and two whole paints, and the
  // call graph `render → sync → watch → onFrameLoad → render` is one refactor
  // away from being a loop. So a load reached FROM a sync does neither: the sync
  // it is inside is already doing both, afterwards, once.
  let syncing = false;

  function sync(): boolean {
    syncing = true;
    try {
      return syncNow();
    } finally {
      syncing = false;
    }
  }

  function syncNow(): boolean {
    if (!opts.hosted) {
      // Split: the frame is ours and can only ever be DETACHED, never
      // re-marked. Still re-resolved, because `enterNoPane` removes it.
      const own = safeMarked(findMarked);
      const moved = own !== frame;
      frame = own;
      isXO = false;
      // ADOPTED HERE TOO, not only in `start()`. `AppPane` mounts the pane's
      // iframe AFTER this component's own effects have run, so the `start()`
      // that looked for it found nothing — and this branch used to be the only
      // thing that ran afterwards, which left the split layout with a marked
      // frame, a painted bar and NO listeners inside the app. `watch` is
      // idempotent per frame and re-enters here, so the assignment above comes
      // first (T:6127).
      if (own) watch(own);
      const d = targetDoc();
      observe(d);
      retargetLayer(d);
      opts.resolveLayer?.(d, false);
      return moved;
    }
    const next = safeMarked(findMarked);
    const moved = next !== frame;
    frame = next;
    // Assigned BEFORE the adoption, because `watch` wires the document already
    // in the frame (nothing else would — its `load` fired long ago) and that
    // wiring re-enters here; it has to find the move already made (T:6127).
    if (next) watch(next);
    const d = targetDoc();
    // A marked frame whose document is unreachable is the cross-origin case; a
    // same-origin frame mid-navigation reads the same for a beat and resolves on
    // the next sync.
    isXO = !!(next && !d);
    observe(d);
    retargetLayer(d);
    opts.resolveLayer?.(d, isXO);
    return moved;
  }

  function watch(f: HTMLIFrameElement | null): void {
    const g = f as (HTMLIFrameElement & AnnGuards) | null;
    if (!g || g.__fusedAnnWatched) return;
    g.__fusedAnnWatched = true;
    g.addEventListener("load", onFrameLoad);
    // The guard and the listener come off TOGETHER: clearing the flag on its own
    // (which is all `releaseGuards` used to do) let the next mount bind a second
    // `load` handler while the first went on firing against a torn-down
    // instance.
    watched.set(g, () => {
      g.removeEventListener("load", onFrameLoad);
      g.__fusedAnnWatched = false;
    });
    // `about:blank` is not a document that was already there, it is the
    // placeholder before the first real one — and its `load` is coming anyway.
    try {
      const w = g.contentWindow;
      if (w && String(w.location.href) !== "about:blank") onFrameLoad();
    } catch {
      /* cross-origin or mid-navigation: nothing to wire */
    }
  }

  /** T:8517 `annWireTarget`. Hosted, the SYNC comes first: a load event here can
   *  be the pane changing mode under us, and the mark may already have moved to
   *  another frame — the one we want is whichever is marked now, never "the one
   *  that fired". */
  function onFrameLoad(): void {
    if (opts.hosted && !syncing) sync();
    const d = targetDoc();
    if (!d || !frame) return;
    // ABOVE the run-once check: the observer is not a listener on this document,
    // it is one instance that has to point at whichever document is the target
    // NOW — and re-selecting an already-wired one is exactly the case the check
    // below skips.
    observe(d);
    // ONE WIRING PER DOCUMENT, and the record of it is the teardown we are
    // holding — not a flag we set on the way in. `wireTarget` sets and clears
    // `__fusedAnnWired` itself; writing it here made its own idempotence check
    // fire on the very first call, so it returned a no-op and none of the six
    // listeners was ever attached.
    if (!wired.has(d)) {
      // THE EXPANDO IS NOT OUR RECORD — `wired` is. `__fusedAnnWired` lives on a
      // document the HOST owns and its only clearer is our own teardown, so an
      // instance that never got to tear down (a crash, a host that dropped the
      // tree without a `pagehide`, or two chat trees alive over one host frame —
      // "the most recently mounted chat owns it") leaves it set. This used to
      // CLEAR it here so `wireTarget` would not return its no-op — which cured
      // the armed-but-dead switch by STACKING a second set of capture-phase
      // click swallowers and mark writers on a live document, the dead
      // instance's still bound and every event firing twice (Bugbot, PR #1074).
      // Nothing to clear now: `wireTarget` is idempotent per document by
      // remove-before-add, so it takes whatever set is there off and hands back
      // the one live teardown. Never write the guard here.
      const off = opts.wireDoc(d, frame);
      if (off) wired.set(d, off);
    }
    // The trailing paint belongs to a load that ARRIVED. Entered from `sync`,
    // the sync is mid-flight and its caller repaints; painting here would make
    // one adoption two full paints (see `syncing`).
    if (!syncing) opts.render();
  }

  /** The layer host is OUR node in a document we do not own: it has to come out
   *  by hand when the target changes, exactly as it does on unmount. */
  function retargetLayer(next: Document | null): void {
    if (next === layerDoc) return;
    if (layerDoc) removeLayer(layerDoc);
    layerDoc = next;
  }

  function disconnect(): void {
    if (observer) observer.disconnect();
    observer = null;
    observedDoc = null;
  }

  /** Everything we installed on objects that outlive us, taken back off. Ordered
   *  documents-then-frames so no load can re-wire a document we just released. */
  function release(): void {
    for (const off of wired.values()) {
      try {
        off();
      } catch {
        /* the document went first */
      }
    }
    wired.clear();
    for (const off of watched.values()) {
      try {
        off();
      } catch {
        /* the frame went first */
      }
    }
    watched.clear();
    // Belt and braces for the guards an instance that never got to tear down
    // (a crash, a host that dropped us mid-frame) may have left set: to the NEXT
    // instance those read as "already adopted" and its wiring never runs.
    const d = targetDoc();
    if (frame) (frame as HTMLIFrameElement & AnnGuards).__fusedAnnWatched = false;
    if (d) (d as Document & AnnGuards).__fusedAnnWired = false;
  }

  function capable(): boolean {
    return opts.hosted ? !!frame : !opts.noPane();
  }

  function poll(): void {
    const had = !!frame;
    const moved = sync();
    const has = !!frame;
    if (has !== lastCapable) {
      lastCapable = has;
      opts.onCapableChange?.(has);
    }
    if (has === had) {
      // Same ANSWER, different FRAME: the shell switched the pane's mode between
      // two iframes it keeps mounted. The switch has nothing to say about that,
      // but the pins do and nobody else is going to draw them — no wiring runs
      // for a frame already adopted, so no render comes with it (T:8461).
      if (moved) opts.render();
      return;
    }
    if (has) opts.onArrive?.();
    else {
      opts.onLeave?.();
      opts.render();
    }
  }

  return {
    frame: () => frame,
    doc: targetDoc,
    xo: () => isXO,
    capable,
    layout(): AnnLayout {
      if (!capable()) return "none";
      if (isXO) return "xo";
      return opts.hosted ? "hosted" : "split";
    },
    sync,
    poll,
    watch,
    start() {
      if (!opts.hosted) {
        // The split layout is told about its pane by the pane itself: one
        // iframe, ours, adopted before it has a src.
        watch(safeMarked(findMarked));
        // The SAME teardown as hosted, minus the poll: the split layout's
        // observer used to be left connected on unmount, because the only path
        // that disconnected it was the hosted `pagehide`.
        return () => {
          release();
          disconnect();
        };
      }
      poll();
      const timer = win0 ? win0.setInterval(poll, ANN_TARGET_POLL_MS) : null;
      win0?.addEventListener("focus", poll);
      return () => {
        if (timer !== null && win0) win0.clearInterval(timer);
        win0?.removeEventListener("focus", poll);
        release();
        disconnect();
      };
    },
    releaseGuards: release,
    removeInjectedLayer() {
      // Both spellings of "where our layer is": the document that is the target
      // NOW, and the one we last injected into (a mark that moved between the
      // two leaves the node behind in the second).
      removeLayer(targetDoc());
      if (layerDoc) removeLayer(layerDoc);
      layerDoc = null;
      // ...and the THIRD home, the cross-origin overlay in the parent's
      // document, which neither of those two lookups can reach.
      opts.removeXOLayer?.();
    },
    disconnectObserver: disconnect,
  };
}

function safeMarked(find: () => HTMLIFrameElement | null): HTMLIFrameElement | null {
  try {
    return find() || null;
  } catch {
    return null;
  }
}
