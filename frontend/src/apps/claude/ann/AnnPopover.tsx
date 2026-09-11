// THE NOTE COMPOSER (`#annpop`, T:3911 the markup, 7233-7480 the behaviour).
//
// ONE NODE that MOVES. An iframe cannot paint outside its own box and the target
// may be a sibling frame elsewhere in the host's layout, so a popover that has
// to appear beside the element it describes has to BE a node in that element's
// document (T:7241). `adoptNode` moves it there; a close brings it home.
//
// The node, not a copy — and imperative, not React. Everything the composer is
// lives on it (the keydown that saves on Enter and closes on Escape, the delete
// button's click, the textarea's current text), and while it is portaled it is
// outside React's root container, where React's delegated events never fire: a
// React-rendered card would go dead the moment it was lent out, and a rebuilt
// one would be a second implementation to keep in step.
import { useEffect, useRef } from "react";

import { popAt, type StageBox } from "./geometry";
import type { AnnAnchor, AnnTool } from "./types";

export interface AnnPopoverHandlers {
  /** T:7416 `annCommit` — SAVE ONLY, nothing is sent from here (Akshil,
   *  2026-09-04): a saved note used to go to Claude the moment Enter landed,
   *  which turned the FIRST note of a review into a run and left the reader
   *  giving feedback to a model already acting on half of it. */
  commit(text: string): void;
  /** T:7443 — one press, and it does NOT leave the mode. */
  close(): void;
  /** T:7409 — the editor's Delete button. */
  del(): void;
}

/** T:3918 — the card, built for whichever document it will first stand in
 *  (ours; it is adopted from there). */
export function buildPopNode(doc: Document, h: AnnPopoverHandlers): HTMLElement {
  const pop = doc.createElement("div");
  pop.id = "annpop";
  pop.className = "c-annpop";
  pop.style.display = "none";

  const ta = doc.createElement("textarea");
  ta.rows = 2;
  // JUST THE WORDS (Akshil, 2026-08-19): the anchor chip and the per-note mic
  // left this card. What a click pins is chosen up front in the Element/Point
  // picker; speaking is the walkthrough's job. The placeholder still names the
  // target's KIND, so the one line of context the chip carried survives.
  ta.placeholder = "What about this element?";
  ta.spellcheck = false;

  const hint = doc.createElement("div");
  hint.className = "hint";
  hint.textContent = "Enter to save · Esc to cancel";
  const del = doc.createElement("button");
  del.id = "anndel";
  del.type = "button";
  del.hidden = true;
  del.textContent = "Delete";
  del.addEventListener("click", () => h.del());
  hint.appendChild(del);

  pop.append(ta, hint);

  ta.addEventListener("keydown", (e) => {
    const ke = e as KeyboardEvent;
    // stopPropagation, because the document-level Escape binding would otherwise
    // see an ALREADY-CLOSED composer (this handler runs first) and fall through
    // to the next claimant, leaving annotate mode as well. One press, one undo.
    if (ke.key === "Escape") {
      ke.stopPropagation();
      h.close();
    }
    if (ke.key === "Enter" && !ke.shiftKey) {
      ke.preventDefault();
      h.commit(ta.value.trim());
    }
  });

  // TYPING IN A PORTALED COMPOSER MUST NOT REACH THE APP (Akshil, 2026-09-04).
  // Hosted, the textarea is a node of the target's document, so every keystroke
  // bubbles out of the layer's shadow root into that document's own listeners —
  // and an app with a document-level "any key focuses the search box" handler
  // pulled focus mid-keystroke, so the letters landed in ITS input and the note
  // stayed empty.
  //
  // On the CARD rather than the textarea because the whole card is the composer,
  // and on the NODE rather than the layer host because the node is what travels.
  // BUBBLE phase, deliberately: a capture listener here would stop the event
  // before it reached the textarea's own keydown handler, and Enter and Escape
  // live there.
  //
  // Two halves. Stopping the bubble is the whole fix for bubble-phase listeners;
  // a CAPTURE-phase listener on the app's document has already run by the time
  // the event gets here, and if it moved focus the character about to be
  // inserted (the default action runs after dispatch) would still land in the
  // app's field. So focus is also taken back, synchronously, on every keydown.
  for (const type of ANN_KEY_EVENTS) {
    pop.addEventListener(type, (e) => {
      // Asked of the EVENT, not only the node: Enter's own handler ran first and
      // may already have brought the card home, while the event is still walking
      // the app document's path. `view` is the window the key landed in.
      const view = (e as UIEvent).view as Window | null;
      if (pop.ownerDocument === doc && !(view && view !== doc.defaultView)) return;
      e.stopPropagation();
      if (type === "keydown" && pop.style.display === "block") {
        const owner = pop.ownerDocument;
        const host = owner.activeElement;
        const deep = host && host.shadowRoot ? host.shadowRoot.activeElement : host;
        if (deep !== ta && !pop.contains(deep)) ta.focus();
      }
    });
  }
  return pop;
}

/** T:7461 — every event class a keystroke can arrive as, including the IME's. */
export const ANN_KEY_EVENTS: readonly string[] = [
  "keydown",
  "keyup",
  "keypress",
  "input",
  "beforeinput",
  "compositionstart",
  "compositionupdate",
  "compositionend",
];

/** T:7229 `annPaintPlaceholder` — the composer's one line of context: the
 *  placeholder names the KIND of thing the note is pinned to. */
export function placeholderFor(anchor: Pick<AnnAnchor, "kind"> | null): string {
  return anchor && anchor.kind === "point" ? "What about this spot?" : "What about this element?";
}

/** T:7238 `annPortaled` — is the card living in someone else's document?
 *  OWNERSHIP is the question, not parentage, and that is what makes the answer
 *  survive everything that can happen to it while it is away: a pane that
 *  reloads leaves it parented to a dead shadow tree, an `adoptNode` that lands
 *  and an `appendChild` that then throws leaves it parented to nothing at all.
 *  `ownerDocument` is what `adoptNode` changes and the only thing still true in
 *  both. */
export function isPortaled(pop: HTMLElement, ownDoc: Document): boolean {
  return pop.ownerDocument !== ownDoc;
}

/** T:7259 `annPortalPop`. Returns whether the move happened: false means there
 *  is nothing to portal INTO — no marked frame, or a pane whose document we
 *  cannot reach — and the caller PARKS the card in the chat column instead. */
export function portalPop(pop: HTMLElement, root: ShadowRoot | null): boolean {
  if (!root) return false;
  if (pop.getRootNode() === root) return true;
  try {
    root.ownerDocument.adoptNode(pop);
    root.appendChild(pop);
  } catch {
    return false; // document torn down mid-gesture: park it instead
  }
  return true;
}

/**
 * T:7291 `annUnportalPop` — home again on every close. Not merely tidiness: a
 * pane that reloads or a mark that moves would otherwise leave an idle composer
 * orphaned in a document nothing points at, and the next open would find it
 * parented to a dead tree.
 *
 * The home is RESOLVED HERE, every time, and never remembered from an earlier
 * visit — remembering it was a race with the no-pane teardown, which removes the
 * very column the card had been parked in, i.e. a composer that opens into
 * nothing. `home` is the chat column; `ownDoc.body` only if that column is
 * somehow gone, because a node dropped nowhere can never be opened again.
 */
export function unportalPop(pop: HTMLElement, ownDoc: Document, home: () => Element | null): void {
  if (isPortaled(pop, ownDoc)) ownDoc.adoptNode(pop);
  // A MOVE WITHIN THIS DOCUMENT COUNTS TOO. The split layout's aimed card is
  // appended into the pane's view box (see `placePop`) — same document, so
  // `isPortaled` is false and the old early return left it parked over the app
  // for ever, under a stylesheet that positions it against a box the closed card
  // is no longer measured in.
  const h = home() || ownDoc.body;
  if (pop.parentNode !== h) h.appendChild(pop);
}

export interface PlacePopOptions {
  pop: HTMLElement;
  /** The framed viewport, split or hosted alike (`annStageEl`). */
  stage: StageBox | null;
  /** The layer's shadow root, or null in the split layout — where the card is
   *  already over the pane and has nowhere to go, which is also what makes the
   *  portal unreachable there without a second flag (T:6041). */
  root: ShadowRoot | null;
  hosted: boolean;
  ownDoc: Document;
  home: () => Element | null;
  /**
   * The split layout's STAGE NODE — the pane's view box, the element the pins
   * and the ring already live in. The card is moved into it for an aimed open,
   * because that box IS the coordinate space `popAt` computes in: parked in the
   * chat column its containing block is `.c-chat`, so the same left/top put the
   * card over the strip's own buttons instead of beside the element it is about
   * (QA round 2, item 3).
   *
   * Hosted and XO have a shadow root to portal into and hand null; a split
   * layout with no pane resolved yet does too, and falls back to the parked
   * seat, which is the honest answer for coordinates measured against nothing.
   */
  stageEl?: () => Element | null;
}

/**
 * T:7323 `annPlacePop` — ONE placement rule for both layouts, because after the
 * portal both are the same problem: the card sits in a box whose coordinate
 * space IS the framed viewport.
 *
 * Hosted adds only the question of WHERE the node lives, and there is exactly
 * one fallback: null coordinates (a chip-edit for a note whose element no longer
 * resolves) or no layer to portal into leave the card PARKED in the chat column,
 * positioned by the `.sidebar` rule. That is the one case where a text box the
 * reader can reach beats a text box beside the right element — because there is
 * no right element to be beside.
 */
export function placePop(x: number | null, y: number | null, o: PlacePopOptions): void {
  const { pop } = o;
  const aimed = Number.isFinite(x) && Number.isFinite(y);
  if (o.hosted && !(aimed && portalPop(pop, o.root))) {
    unportalPop(pop, o.ownDoc, o.home);
    pop.classList.add("sidebar");
    pop.style.display = "block";
    pop.style.left = ""; // parked: the stylesheet spans the column
    pop.style.top = "";
    focusTa(pop);
    return;
  }
  // SPLIT: no shadow root to portal into, but the same problem — the card has to
  // stand in the box its coordinates are measured against. One `appendChild`,
  // same document, and `unportalPop` brings it home on every close.
  // No stage yet (a pane that has not resolved, or none at all) leaves the card
  // where it is parked: coordinates measured against nothing are not a reason to
  // move it somewhere new.
  const stage = o.hosted ? null : (o.stageEl?.() ?? null);
  if (stage && pop.parentNode !== stage) stage.appendChild(pop);
  pop.classList.remove("sidebar");
  const at = popAt(x as number, y as number, o.stage ?? { clientWidth: 0, clientHeight: 0 });
  pop.style.display = "block";
  pop.style.left = at.left + "px";
  pop.style.top = at.top + "px";
  // Across the frame boundary, hosted — which is allowed (same origin) and is
  // what the reader expects: their pointer is already in the pane, and the
  // textarea they are about to type in is now a node of that pane's document.
  focusTa(pop);
}

function taOf(pop: HTMLElement): HTMLTextAreaElement | null {
  return pop.querySelector("textarea");
}

function focusTa(pop: HTMLElement): void {
  taOf(pop)?.focus();
}

/** T:7355 `annOpenComposer` — a NEW note. */
export function openComposer(anchor: AnnAnchor, x: number, y: number, o: PlacePopOptions): void {
  const ta = taOf(o.pop);
  const del = o.pop.querySelector("#anndel") as HTMLButtonElement | null;
  if (del) del.hidden = true;
  if (ta) {
    ta.placeholder = placeholderFor(anchor);
    ta.value = "";
  }
  placePop(x, y, o);
}

/** T:7365 `annOpenEditor` — reopen a PENDING note (a sent one is already in the
 *  transcript and cannot be recalled; its pin is inert). The anchor stays as
 *  captured; only the text is editable. */
export function openEditor(
  note: { content: string; kind?: string },
  x: number | null,
  y: number | null,
  o: PlacePopOptions,
): void {
  const ta = taOf(o.pop);
  const del = o.pop.querySelector("#anndel") as HTMLButtonElement | null;
  if (del) del.hidden = false;
  if (ta) {
    ta.placeholder = placeholderFor(note as AnnAnchor);
    ta.value = note.content;
  }
  placePop(x, y, o);
  // Cursor at the END, nothing selected — a stray keypress must append, not wipe
  // the note.
  if (ta) ta.setSelectionRange(ta.value.length, ta.value.length);
}

/** T:7381 `annCloseComposer`. Home before the next open, ALWAYS — including the
 *  closes that are not the reader's (a disarm, a chip deleted out from under the
 *  editor, the sync noticing the pane it was lent to is gone). */
export function closeComposer(o: PlacePopOptions): void {
  o.pop.style.display = "none";
  unportalPop(o.pop, o.ownDoc, o.home);
}

export function isOpen(pop: HTMLElement | null): boolean {
  return !!pop && pop.style.display === "block";
}

/** T:7395 — the chat document's half of the outside-click dismissal, and the
 *  exemptions it took two bugs to complete: ✓ Done on the STRIP (Bugbot #664)
 *  and ✓ Done on the BAR (Bugbot #1008) both used to close the composer on
 *  mousedown, before the click could reach the handler, silently dropping the
 *  words the user was about to send. The picker rides along — choosing the next
 *  click's tool is not walking away from this note. */
export const ANN_DISMISS_EXEMPT: readonly string[] = [
  // `.annpin` — the class `paintPins` actually writes, in BOTH layouts (the
  // split layout's pins are ordinary nodes of this document, the hosted ones
  // are retargeted to the layer host). `.c-annpin` matched nothing, so a
  // mousedown on a pin dismissed the composer before the pin's own click could
  // toggle it closed.
  ".annpin",
  ".c-annchip",
  ".c-anncta",
  "#anntool",
  ".annbar",
];

export function dismissesComposer(pop: HTMLElement, target: EventTarget | null): boolean {
  const t = target as Element | null;
  if (!isOpen(pop)) return false;
  if (!t) return true;
  if (pop.contains(t)) return false;
  if (!t.closest) return true;
  return !ANN_DISMISS_EXEMPT.some((sel) => t.closest(sel));
}

// ── the mount ───────────────────────────────────────────────────────────────

export interface AnnPopoverProps {
  handlers: AnnPopoverHandlers;
  /** Handed the live node once, so the coordinator can place, portal and read
   *  it. Null on unmount. */
  popRef?: (pop: HTMLElement | null) => void;
}

/**
 * The composer's IDLE HOME: a seat in the chat column that holds the one node
 * while it is not lent to anybody. React renders the seat; the node inside it is
 * `buildPopNode`'s, for the reasons at the top of this file.
 */
export function AnnPopover({ handlers, popRef }: AnnPopoverProps) {
  const host = useRef<HTMLDivElement | null>(null);
  const live = useRef({ handlers, popRef });
  live.current = { handlers, popRef };

  useEffect(() => {
    const h = host.current;
    if (!h) return;
    const node = buildPopNode(h.ownerDocument, {
      commit: (text) => live.current.handlers.commit(text),
      close: () => live.current.handlers.close(),
      del: () => live.current.handlers.del(),
    });
    h.appendChild(node);
    live.current.popRef?.(node);
    return () => {
      live.current.popRef?.(null);
      // Brought home before it is dropped: a node left in the app's document
      // with nothing holding it is the orphan `unportalPop` exists to prevent.
      unportalPop(node, h.ownerDocument, () => h);
      node.remove();
    };
  }, []);

  return <div className="c-annpop-host" ref={host} />;
}

// ── the Element/Point picker ────────────────────────────────────────────────

const IC_ELEMENT =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="2.5"/><rect class="fill" x="6" y="6" width="4" height="4" rx="1"/></svg>';
const IC_POINT =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3.25"/><line x1="8" y1="1.5" x2="8" y2="4"/><line x1="8" y1="12" x2="8" y2="14.5"/><line x1="1.5" y1="8" x2="4" y2="8"/><line x1="12" y1="8" x2="14.5" y2="8"/></svg>';

/**
 * T:3955 `#anntool` — a RADIOGROUP, not two toggles: exactly one is ever the
 * answer. Imperative for the same reason the bar and the card are: it RIDES the
 * bar into the app's document (`paintBar` adopts it), where React's delegated
 * events do not fire.
 *
 * A picker for a click that cannot happen is noise, so it appears only while the
 * mode is armed — typed comment and recording alike (Akshil, 2026-08-19) — and
 * never over a cross-origin target, where every click is a spot (D355).
 */
export function buildToolNode(doc: Document): HTMLElement {
  const group = doc.createElement("div");
  group.id = "anntool";
  group.setAttribute("role", "radiogroup");
  group.setAttribute("aria-label", "What a click pins");
  group.hidden = true;
  const seat = (id: string, label: string, tip: string, glyph: string, checked: boolean) => {
    const b = doc.createElement("button");
    b.id = id;
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(checked));
    b.setAttribute("aria-label", label);
    b.dataset.tip = tip;
    b.innerHTML = glyph + '<span class="lbl">' + label + "</span>";
    return b;
  };
  group.append(
    seat("toolel", "Element", "Clicks pin the element under the cursor", IC_ELEMENT, true),
    seat(
      "toolpt",
      "Point",
      "Clicks pin the exact spot, with a marked screenshot",
      IC_POINT,
      false,
    ),
  );
  return group;
}

/** T:6558 `annSetTool`. */
export function paintTool(group: HTMLElement, tool: AnnTool): void {
  group.querySelector("#toolel")?.setAttribute("aria-checked", String(tool === "element"));
  group.querySelector("#toolpt")?.setAttribute("aria-checked", String(tool === "point"));
}

/** T:6566 `annToolClick` — ONE named handler on the GROUP, not two closures on
 *  the buttons: it is re-attached after every adoption, which only a stable
 *  reference makes idempotent. */
export function toolFromClick(e: Event): AnnTool | null {
  const t = e.target as Element | null;
  const b = t && t.closest ? t.closest("button") : null;
  if (!b) return null;
  if (b.id === "toolel") return "element";
  if (b.id === "toolpt") return "point";
  return null;
}

/** T:7500 — the picker's two doors, sharing the exit animation: SHOWING is the
 *  display flip itself (leaving `hidden` restarts the CSS entry animation),
 *  HIDING holds the flip up for the `.out` glide back into the buttons. The
 *  timer is kept so a show that lands mid-exit (stop, then instantly record
 *  again) cancels the pending hide instead of being hidden by it. */
export function createToolDoors(
  group: () => HTMLElement | null,
  setTimer: (fn: () => void, ms: number) => number = (fn, ms) => window.setTimeout(fn, ms),
  clearTimer: (id: number) => void = (id) => window.clearTimeout(id),
): { show(): void; hide(): void } {
  let timer: number | null = null;
  return {
    show() {
      const g = group();
      if (!g) return;
      if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
      g.classList.remove("out");
      g.hidden = false;
    },
    hide() {
      const g = group();
      if (!g || g.hidden || timer !== null) return;
      g.classList.add("out");
      timer = setTimer(() => {
        timer = null;
        g.hidden = true;
        g.classList.remove("out");
      }, 150);
    },
  };
}

export default AnnPopover;
