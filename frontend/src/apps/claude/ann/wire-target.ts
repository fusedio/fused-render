// THE SEVEN LISTENERS INSIDE THE FRAMED DOCUMENT (T:8534-8710).
//
// Nothing that happens in an iframe bubbles out of it: a keydown, a mousedown, a
// click and a scroll in the app all land in the app's document and nowhere else.
// So annotate mode is not "a listener on the chat that watches the pane" — it is
// seven listeners over there, attached on every document LOAD (the app live-reloads
// on Claude's edits) and gating on the mode, so toggling never needs a rewire.
//
// ONE WIRING PER DOCUMENT, marked on the document itself: a reload brings a fresh
// document and must be wired again, while re-adopting a frame we already wired —
// the mark leaving and coming back as the reader switches the pane's mode to and
// fro — must not stack a second set of listeners on it (T:8524).
import { contentBox, intrinsicOf, iuivAt, pageXY, pathOf } from "./geometry";
import { hideHl, placeHl } from "./layer";
import { ANN_LAYER_MARK, type AnnAnchor, type AnnTool } from "./types";

/** Our own expandos on the host's document (T:8524).
 *
 *  `__fusedAnnWired` is the public boolean `isWired` reads; `__fusedAnnOff` is
 *  THE RECORD of the one listener set currently on this document — the teardown
 *  that takes it back off. It lives on the document and not only in the caller's
 *  map because the set can outlive the instance that attached it (a crash, a host
 *  that dropped the tree without a `pagehide`, or two chat trees alive over one
 *  host frame), and the next wiring has to be able to REMOVE it rather than
 *  stack on top of it. */
interface WiredDoc {
  __fusedAnnWired?: boolean;
  __fusedAnnOff?: () => void;
}

export interface WireTargetDeps {
  /** The mode is armed (comment OR recording): every handler gates on it. */
  armed(): boolean;
  /** A walkthrough is live: a click IS the note, immediately, with no composer —
   *  the whole point of talking instead of typing is that nothing grabs focus. */
  recording(): boolean;
  /**
   * THE MODE IS ARMED BUT THE CLICKS ARE OVER — `settling`/`transcribing`, the
   * "Stopping…"/"Transcribing…" seat. `armed()` is still true (the
   * transcription belongs to this chat and holds the nav lock) while
   * `recording()` is already false, so every handler below used to read this
   * as Comment mode: a click in the app was swallowed AND opened a composer,
   * over a bar that is hidden and a round that is already closed (Bugbot,
   * PR #1074).
   *
   * Optional: a caller that cannot see the phase gets the old reading.
   */
  settling?(): boolean;
  tool(): AnnTool;
  composerOpen(): boolean;
  /** The ring, in whichever layer is current. Re-read per event: hosted it lives
   *  in the target's document and that document may have been replaced. */
  hl(): HTMLElement | null;
  /** The chat's own Escape claimant, bound here too — annotate mode is used with
   *  the pointer (and usually focus) IN here, and keydowns do not cross the
   *  frame boundary (T:8534). */
  onEscape(e: KeyboardEvent): void;
  closeComposer(): void;
  openComposer(x: number, y: number, anchor: AnnAnchor): void;
  /** A stamped, wordless note at an exact spot (T:7972 `annRecMarkPoint`). */
  markPoint(clientX: number, clientY: number, win: Window | null, nearPath?: string): void;
  /** A stamped, wordless note on an element (T:7952 `annRecMark`). */
  mark(anchor: AnnAnchor): void;
  /** rAF-coalesced repaint for the scroll listener. */
  queueRender(): void;
}

/**
 * Wire one document. Returns the teardown, which also clears the guard — so a
 * React unmount leaves the document as it found it rather than as "already
 * wired" with handlers that no longer run (T:8802's release, generalised).
 *
 * IDEMPOTENT PER DOCUMENT, by REMOVE BEFORE ADD (Bugbot, PR #1074). N calls
 * leave SEVEN listeners and one live teardown, whoever made them: the set
 * already on the document comes off first. It cannot be a bare
 * "already wired, do nothing" — the guard lives on a document the HOST owns and
 * an instance that never got to tear down leaves it set, so the no-op return
 * this used to take handed the next mount an armed-but-dead switch with nothing
 * attached and no console error. Nor can the caller just CLEAR the guard and
 * wire again (`target.ts` did): the previous instance's capture-phase click
 * swallowers and mark writers stay bound, and every event fires twice — once
 * into a torn-down tree.
 */
export function wireTarget(doc: Document, deps: WireTargetDeps): () => void {
  const guard = doc as Document & WiredDoc;
  // REMOVE BEFORE ADD. Its own teardown clears both expandos, so what follows
  // starts from a document with no wiring of ours on it at all.
  const prev = guard.__fusedAnnOff;
  if (prev) {
    try {
      prev();
    } catch {
      /* the previous instance's document surface went first: nothing to remove */
    }
  }
  guard.__fusedAnnWired = true;

  const elementOf = (t: EventTarget | null): Element | null => {
    const n = t as Node | null;
    if (!n) return null;
    return n.nodeType === 1 ? (n as Element) : ((n as ChildNode).parentElement ?? null);
  };

  /**
   * Our own injected layer, seen from INSIDE the app's document. In the split
   * layout no such node exists (the pins are in the chat's document, which the
   * app's handlers cannot see) and this is always false.
   *
   * The COMPOSER is inside that layer while it is portaled, and the shadow
   * boundary is what makes one test cover both: an event raised in the shadow
   * tree is retargeted to the HOST for every listener out here, so a mousedown
   * in the textarea, a hover over the card and a click on the delete button all
   * arrive as the layer host — already marked, already exempt (T:8556).
   */
  const ownNode = (t: EventTarget | null): boolean => {
    const el = t as Element | null;
    return !!(el && el.closest && el.closest("[" + ANN_LAYER_MARK + "]"));
  };

  /**
   * ARMED, AND STILL TAKING CLICKS. The gate every handler reads instead of
   * `armed()`: through the settle the mode is armed but the gesture is over, so
   * a click there belongs to the APP again — passed through, un-swallowed, and
   * opening nothing (Bugbot, PR #1074).
   */
  const live = (): boolean => deps.armed() && !(deps.settling?.() ?? false);

  /**
   * THE AIM SIGNAL. While the click would pin a SPOT — the Point tool is in hand,
   * or Alt overrides the Element tool for this click — the cursor is a crosshair
   * and the ring goes away; the pointer says what the click will do before it
   * commits. (Point tool + Alt is the element again, so the ring is back: the
   * override works in both directions, the same XOR the click handler uses.)
   *
   * Written on the APP's root, because the pointer is over the app's own nodes,
   * and re-derived on every move and on Alt's keyup, so neither a released key
   * nor a tool switch can strand it (T:8579).
   */
  const aimCursor = (on: boolean): void => {
    const root = doc.documentElement as HTMLElement;
    if (on && root.style.cursor !== "crosshair") root.style.cursor = "crosshair";
    else if (!on && root.style.cursor === "crosshair") root.style.cursor = "";
  };

  // ── 1. keydown: Escape, which cannot reach the chat from in here ──────────
  const onKeyDown = (e: Event) => deps.onEscape(e as KeyboardEvent);

  /**
   * THE SWALLOW THE CLICK CANNOT DO ON ITS OWN (A11, QA round 2). A native form
   * control does not wait for the click to change its value: `<input
   * type=range>` sets itself from the POINTER — mousedown places the thumb and
   * the drag keeps moving it — so a click cancelled after the fact arrives long
   * after the app's own state has moved. Measured: clicking the app's `#freq`
   * slider while placing an Element note both opened the composer AND dragged
   * the slider from 1.0 to 2.6.
   *
   * So the pointer's own two events are cancelled in the capture phase as well,
   * which is the same promise the click already makes: while a mode is armed, a
   * click on the app is a NOTE and never also an interaction. `pointerdown` is
   * the modern default action's event and `mousedown` the compatibility one —
   * both, because a browser may fire either alone.
   *
   * NOT gated on "is this a form control": every element whose default action
   * the pointer starts (a drag, a text selection, a native scrollbar) is the
   * same case, and a list of tag names would be a second, always-incomplete
   * model of what the platform does on a pointerdown.
   */
  const swallowPointer = (e: Event): void => {
    if (!live()) return;
    if (ownNode(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
  };

  // ── 2. pointerdown (CAPTURE): the value change that beats the click ───────
  const onPointerDown = (e: Event) => swallowPointer(e);

  // ── 3. mousedown (CAPTURE): the outside-click dismissal, and the swallow ──
  // Hosted this is the dismissal that MATTERS, not a mirror of the chat's: the
  // composer is portaled into this very document, so every click that ought to
  // dismiss it lands here and nowhere else. Exempt exactly as the pins and chips
  // are on the chat's side: a mousedown on a pin is the start of that pin's
  // toggle, and closing here would turn the toggle into a reopen.
  //
  // The dismissal runs FIRST and the swallow second: closing the composer is
  // ours to do whichever way the mode reads, and cancelling the event must not
  // cancel that.
  const onMouseDown = (e: Event) => {
    if (ownNode(e.target)) return;
    if (deps.composerOpen()) deps.closeComposer();
    swallowPointer(e);
  };

  // ── 4. keyup Alt: the momentary override, released ────────────────────────
  const onKeyUp = (e: Event) => {
    const ke = e as KeyboardEvent;
    if (ke.key !== "Alt") return;
    const aimPoint = live() && deps.tool() === "point";
    aimCursor(aimPoint);
    // The ring Alt brought back (the element override on the Point tool) must go
    // with the key: leaving it until the next mousemove has the crosshair and the
    // ring disagreeing about what the next click pins. But an OPEN composer OWNS
    // the ring — it is frozen on the element being written about, and Point+Alt
    // is exactly how that element gets pinned, so releasing Alt afterwards must
    // not strip the ring off the note.
    if (aimPoint && !deps.composerOpen()) hideHl(deps.hl());
  };

  // ── 5. mousemove: the hover ring follows what the click would take ────────
  const onMouseMove = (e: Event) => {
    const me = e as MouseEvent;
    if (!live()) {
      aimCursor(false);
      return;
    }
    const aimPoint = (deps.tool() === "point") !== me.altKey;
    aimCursor(aimPoint);
    if (aimPoint) {
      hideHl(deps.hl());
      return;
    }
    // Hovering our own pin is not hovering the app: leave the ring on whatever it
    // was marking rather than jumping it to a 22px circle of ours.
    if (ownNode(me.target)) return;
    // An open composer means the user CLICKED an element and is writing about it:
    // the highlight is that element's, and a hover that drags it away makes the
    // popover describe one box while the ring shows another. FROZEN, not
    // re-anchored — the composer does not follow scroll either, so the pair moves
    // (and closes) together.
    if (deps.composerOpen()) return;
    const el = elementOf(me.target);
    if (!el || el === doc.body || el === doc.documentElement) {
      hideHl(deps.hl());
      return;
    }
    placeHl(deps.hl(), el);
  };

  // ── 6. click (CAPTURE): everything swallowed while annotating ─────────────
  // Buttons and links included: annotate mode exists precisely to point at
  // controls WITHOUT triggering them. Toggle off to use the app normally.
  const onClick = (e: Event) => {
    const me = e as MouseEvent;
    if (!live()) return;
    // A pin of ours, hosted, is the one thing in this document that must reach
    // its own handler: swallowing it here would eat the click that reopens the
    // note's editor and then anchor a NEW note to the pin marking the old one.
    if (ownNode(me.target)) return;
    me.preventDefault();
    me.stopPropagation();
    const el = elementOf(me.target);
    const win = doc.defaultView;
    // The click's OTHER meaning: an exact spot, in PAGE coordinates. Built for
    // every click, because every click can name one — it is the note when there
    // is no element to anchor to and when the reader forces it.
    const pointAnchor: AnnAnchor = { kind: "point", ...pageXY(me.clientX, me.clientY, win) };
    const asPoint = (nearPath?: string) => {
      if (deps.recording()) {
        deps.markPoint(me.clientX, me.clientY, win, nearPath);
        return;
      }
      hideHl(deps.hl());
      deps.openComposer(
        me.clientX,
        me.clientY,
        nearPath ? { ...pointAnchor, nearPath } : pointAnchor,
      );
    };
    // Nothing under the click: a point note — narration about whitespace, layout,
    // or something MISSING deserves a note as much as a button does.
    if (!el || el === doc.body || el === doc.documentElement) {
      asPoint();
      return;
    }
    const anchor: AnnAnchor = el.id ? { anchorId: el.id } : { anchorPath: pathOf(el, doc) ?? undefined };
    // An element we cannot NAME (a shadow tree `pathOf` cannot walk) is an
    // element no note could ever resolve back to: the spot is the honest anchor.
    if (!anchor.anchorId && !anchor.anchorPath) {
      asPoint();
      return;
    }
    // A click on a PIXEL SURFACE — and only on one — records the exact spot
    // inside the painted content box, so the note marks the pixel and not the
    // box (T:8669). Gated on the intrinsic size, because `iu`/`iv` are fractions
    // OF THE PICTURE: on an ordinary `<div>` they would be fractions of a box
    // that reflows, i.e. a coordinate that means something different every time
    // the layout changes, and the element anchor already says the box.
    if (intrinsicOf(el)) {
      const iuiv = iuivAt(me.clientX, me.clientY, contentBox(el));
      if (iuiv) {
        anchor.iu = iuiv.iu;
        anchor.iv = iuiv.iv;
      }
    }
    // A short element digest so Claude can find the source without resolving the
    // path first: tag plus the element's leading text.
    anchor.tag = el.tagName.toLowerCase();
    const t = (el.textContent || "").trim().replace(/\s+/g, " ");
    if (t) anchor.text = t.slice(0, 80);
    // Is THIS click a point? The tool in hand says so, or Alt says so for just
    // this click — the momentary override of EITHER tool, so Point-tool + Alt
    // means the element and the exception works in both directions.
    const wantPoint = (deps.tool() === "point") !== me.altKey;
    // A FORCED point that landed over a real element names it. ONE field for both
    // spellings rather than a second `nearId`, because both are the CSS-selector
    // scheme `pathOf` already writes (D146 forbids a second implementation, not a
    // second spelling of one) — and it is a HINT, never the anchor, or a note
    // about the space beside a button becomes a note about the button.
    const nearPath = anchor.anchorId ? "#" + anchor.anchorId : anchor.anchorPath;
    if (wantPoint) {
      asPoint(nearPath);
      return;
    }
    if (deps.recording()) {
      placeHl(deps.hl(), el);
      deps.mark(anchor);
      return;
    }
    // Re-anchor the ring to what was CLICKED before freezing on it: the hover
    // follow stops while a composer is open, so on a click that moves the popover
    // the last-painted ring belongs to the previous element.
    placeHl(deps.hl(), el);
    deps.openComposer(me.clientX, me.clientY, anchor);
  };

  // ── 7. scroll (CAPTURE, PASSIVE): the pins move with the content ──────────
  const onScroll = () => deps.queueRender();

  doc.addEventListener("keydown", onKeyDown);
  doc.addEventListener("pointerdown", onPointerDown, true);
  doc.addEventListener("mousedown", onMouseDown, true);
  doc.addEventListener("keyup", onKeyUp);
  doc.addEventListener("mousemove", onMouseMove);
  doc.addEventListener("click", onClick, true);
  doc.addEventListener("scroll", onScroll, { capture: true, passive: true });

  const off = () => {
    doc.removeEventListener("keydown", onKeyDown);
    doc.removeEventListener("pointerdown", onPointerDown, true);
    doc.removeEventListener("mousedown", onMouseDown, true);
    doc.removeEventListener("keyup", onKeyUp);
    doc.removeEventListener("mousemove", onMouseMove);
    doc.removeEventListener("click", onClick, true);
    doc.removeEventListener("scroll", onScroll, { capture: true });
    aimCursor(false);
    // ONLY OURS TO CLEAR. A later wiring replaced the record with its own, and a
    // stale teardown arriving after it (a React unmount that lost the race)
    // must not tell the document it is unwired while those seven are live.
    if (guard.__fusedAnnOff === off) {
      guard.__fusedAnnWired = false;
      delete guard.__fusedAnnOff;
    }
  };
  guard.__fusedAnnOff = off;
  return off;
}

/** T:8496 — is this document already wired? Exposed for the poll, which asks
 *  before it pays for a sync. */
export function isWired(doc: Document | null): boolean {
  return !!(doc && (doc as Document & WiredDoc).__fusedAnnWired);
}
