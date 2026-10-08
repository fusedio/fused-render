// Input forwarding for the live view while you drive: pointer and wheel from the stage, keys from #fkeys (a hidden
// textarea: only an editable element composes dead keys and IMEs, which land as one Input.insertText on compositionend),
// HTML5 drags replayed through Input.dragIntercepted with a ghost image, ⌘C/⌘X copied to YOUR clipboard (measured:
// headless Chrome's clipboard is private to it). `installLive` wires it all once (LiveView) and returns the teardown.
//
// Ordering rules. A left press first asks the page what is under it (a closed <select> or a picker opens an overlay of
// ours instead of Chrome's invisible popup), so presses are a promise chain and moves / the release queue behind the
// press they belong to; keys go through one serial queue too, since a few (Space, Enter, arrows, ⌘C) also wait on a
// probe. Nothing reaches the page out of order, and a release is always matched to our own press, never to where the
// pointer happens to be when the button comes up.
import { cur, getState, poll, subscribe as subscribeStore } from "../state/store";
import { $, askTakeOver, cdp, evalIn, frameMeta, handBack, inCtl, inFull, isSwitching, linkSync, nav, onEvent, onReset, takeOver, followPopups } from "./cdp";
import { BTN, CDP_MODS, frameDims, heldButton, keyAction, nextDown, toPageXY, typesText, wrapIndex, type Box, type KeyAction, type LastDown } from "./live";
import { syncDriving } from "./live-page";
import { SEL_TEXT_PROBE, cancelSuggest, closeOverlay, copyText, getOverlay, openContextAt, openOverlayAt, pickSelect, scheduleSuggest, setOverlay, stageXY, toStage } from "./live-overlays";

// Map to CSS viewport pixels (what CDP expects): the frame's own metadata, else the bot's viewport.
function toPage(e: MouseEvent): { x: number; y: number } | null {
  const img = $<HTMLImageElement>("fshot"); if (!img) return null;
  return toPageXY(e.clientX, e.clientY, img.getBoundingClientRect(), [img.naturalWidth, img.naturalHeight], frameMeta(), cur()?.viewport);
}
// `buttons` (the held-button bitmask, same encoding in DOM and CDP) is what lets Chrome see a drag and a right-button press.
const mouse = (type: string, p: { x: number; y: number }, e: MouseEvent, extra: object = {}) =>
  cdp("Input.dispatchMouseEvent", { type, x: p.x, y: p.y, modifiers: CDP_MODS(e), buttons: e.buttons, ...extra });
function sendKey(a: KeyAction): void {
  if (a?.kind === "key") cdp("Input.dispatchKeyEvent", a.params);
  else if (a?.kind === "insert") cdp("Input.insertText", { text: a.text });
}

// ------------------------------------------------------------------ drag + ghost ----
/** An intercepted HTML5 drag in flight: its DragData, replayed as dragEnter/dragOver on moves and drop on release. */
let drag: { data: object; entered: boolean } | null = null;
// Chrome's OS drag loop would draw the dragged element; an intercepted drag has none, so the live view cuts it out of the
// current frame (#fghost) and floats it under the pointer until the drop. Positioned by hand, one style write per move.
let ghost: { dx: number; dy: number } | null = null;
let lastStage = { x: 0, y: 0 };      // the pointer's last stage-relative position
let lastDownPage = { x: 0, y: 0 };   // where the current press landed in the page
async function showGhost(): Promise<void> {
  const start = lastDownPage;
  const rect = await evalIn<Box | null>(`(() => { const el = document.elementFromPoint(${start.x}, ${start.y}); if (!el) return null;
    const d = el.closest("[draggable=true], a[href], img") || el; const r = d.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()`);
  const g = $<HTMLImageElement>("fghost"), img = $<HTMLImageElement>("fshot"), stage = $("stage");
  if (!rect || !g || !img || !stage || !drag) return;
  const box = toStage(rect); if (!box || box.width < 2 || box.height < 2) return;
  const [vw, vh] = frameDims(frameMeta(), cur()?.viewport, [img.naturalWidth, img.naturalHeight]);
  const kx = img.naturalWidth / (vw || 1), ky = img.naturalHeight / (vh || 1);  // frame px per CSS px
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(rect[2] * kx)); c.height = Math.max(1, Math.round(rect[3] * ky));
  try { c.getContext("2d")?.drawImage(img, rect[0] * kx, rect[1] * ky, rect[2] * kx, rect[3] * ky, 0, 0, c.width, c.height); g.src = c.toDataURL("image/png"); }
  catch { return; }  // a tainted frame cannot be read back; drag on without an image
  g.style.width = `${box.width}px`; g.style.height = `${box.height}px`;
  const down = toStage([start.x, start.y, 0, 0])!;
  ghost = { dx: down.left - box.left, dy: down.top - box.top };
  moveGhost();
  g.hidden = false; stage.classList.add("dragging");
}
function moveGhost(): void {
  const g = $<HTMLImageElement>("fghost");
  if (g && ghost) { g.style.left = `${lastStage.x - ghost.dx}px`; g.style.top = `${lastStage.y - ghost.dy}px`; }
}
function endDrag(): void {
  drag = null; ghost = null;
  const g = $<HTMLImageElement>("fghost"); if (g) { g.hidden = true; g.removeAttribute("src"); }
  $("stage")?.classList.remove("dragging");
}
onReset.push(endDrag);

/** Everything that only belongs to a take-over: menus, a drag in flight, the ghost. */
function endDriving(): void { setOverlay(null); cancelSuggest(); endDrag(); }

// ------------------------------------------------------------------ wiring ----
export function installLive(stage: HTMLElement): () => void {
  const keys = $<HTMLTextAreaElement>("fkeys");
  // Our overlays live inside the stage: pointer events in them are theirs, never page input.
  const inMenu = (e: Event): boolean => !!(e.target as Element | null)?.closest?.(".lvov");

  // --- pointer ---
  let lastDown: LastDown = { t: 0, x: 0, y: 0, n: 0 };
  /** The press in flight: sent (or not) once its hit probe answered; the release decides from this, not from the pointer. */
  let press: { forwarded: boolean; button: string; n: number } | null = null;
  let pressQ: Promise<void> = Promise.resolve();
  // Moves are coalesced to one per animation frame: hover menus stay responsive without flooding the socket.
  let pendingMove: { p: { x: number; y: number }; e: MouseEvent } | null = null;
  const onMove = (e: MouseEvent) => {
    if (!inCtl() || inMenu(e)) return;
    const p = toPage(e); if (!p) return;
    lastStage = stageXY(e);
    if (ghost) moveGhost();
    const first = !pendingMove; pendingMove = { p, e };
    if (first) requestAnimationFrame(() => {
      const m = pendingMove; pendingMove = null; if (!m) return;
      void pressQ.then(() => {
        if (!inCtl()) return;
        // Chrome starts a drag only from moves that name the held button (measured: `buttons: 1` alone never fires dragIntercepted).
        mouse("mouseMoved", m.p, m.e, { button: heldButton(m.e.buttons) });
        if (drag) {
          cdp("Input.dispatchDragEvent", { type: drag.entered ? "dragOver" : "dragEnter", x: m.p.x, y: m.p.y, data: drag.data, modifiers: CDP_MODS(m.e) });
          drag.entered = true;
        }
      });
    });
  };
  const onDown = (e: MouseEvent) => {
    if (!inCtl() || inMenu(e)) return;
    const p = toPage(e); if (!p) return;
    e.preventDefault(); keys?.focus({ preventScroll: true });
    setOverlay(null); cancelSuggest();
    lastStage = stageXY(e); lastDownPage = p;
    lastDown = nextDown(lastDown, performance.now(), p);
    const rec = { forwarded: false, button: BTN[e.button] || "left", n: lastDown.n };
    press = rec;
    pressQ = pressQ.then(async () => {
      if (!inCtl()) return;
      if (e.button === 2) {  // the page gets its right-click (custom menus) and ours opens over it
        mouse("mousePressed", p, e, { button: "right", clickCount: rec.n }); rec.forwarded = true;
        void openContextAt(p, e);
        return;
      }
      if (e.button === 0 && rec.n === 1 && await openOverlayAt(p)) return;  // a menu of ours instead of the press
      mouse("mousePressed", p, e, { button: rec.button, clickCount: rec.n }); rec.forwarded = true;
    });
  };
  // The release is matched to our press (so it is heard anywhere: over an overlay, the top bar, or outside the window).
  const onUp = (e: MouseEvent) => {
    const rec = press; if (!rec) return;
    press = null;
    const p = toPage(e) || { x: lastDown.x, y: lastDown.y };
    void pressQ.then(() => {
      if (!rec.forwarded || !inCtl()) { endDrag(); return; }
      if (drag) {
        if (!drag.entered) cdp("Input.dispatchDragEvent", { type: "dragEnter", x: p.x, y: p.y, data: drag.data, modifiers: CDP_MODS(e) });
        cdp("Input.dispatchDragEvent", { type: "drop", x: p.x, y: p.y, data: drag.data, modifiers: CDP_MODS(e) });
      }
      endDrag();
      mouse("mouseReleased", p, e, { button: rec.button, clickCount: rec.n });
      setTimeout(poll, 700);  // a click may open a tab or change the title
    });
  };
  onEvent("Input.dragIntercepted", (p) => {
    if (!inCtl()) return;
    if (!press) { cdp("Input.dispatchDragEvent", { type: "dragCancel", x: lastDownPage.x, y: lastDownPage.y, data: p.data }); return; }  // released before Chrome noticed
    drag = { data: p.data as object, entered: false };
    void showGhost();
  });
  const onCtx = (e: MouseEvent) => { if (inCtl() && !inMenu(e)) e.preventDefault(); };
  // While the bot drives, a click on the page does nothing to it; offer to take over instead of silently ignoring the click.
  const onClick = async (e: MouseEvent) => {
    const b = cur();
    if (!b || inCtl() || isSwitching() || !inFull() || inMenu(e) || !toPage(e)) return;
    if (b.control || await askTakeOver("The bot pauses and you drive this page yourself. Hand back whenever you are done.")) void takeOver();
  };
  const onWheel = (e: WheelEvent) => { if (!inCtl() || inMenu(e)) return; const p = toPage(e); if (!p) return; e.preventDefault(); mouse("mouseWheel", p, e, { deltaX: e.deltaX, deltaY: e.deltaY }); };

  // --- keyboard ---
  let keyQ: Promise<void> = Promise.resolve();
  const queue = (f: () => Promise<void> | void) => { keyQ = keyQ.then(f).catch(() => {}); };
  const onPaste = (e: ClipboardEvent) => {
    if (!inCtl() || inMenu(e)) return;
    const text = e.clipboardData?.getData("text/plain");
    if (!text) return;
    e.preventDefault();
    queue(() => cdp("Input.insertText", { text }));
  };
  const keyEv = (e: KeyboardEvent) => {
    if (!inFull() || inMenu(e) || document.activeElement === $("furl")) return;
    const k = e.key.toLowerCase(), down = e.type === "keydown";
    if (e.metaKey && ["w", "t", "q", "n", "l"].includes(k)) return;
    if (!inCtl()) { if (keys) keys.value = ""; return; }
    if ((e.metaKey || e.ctrlKey) && k === "v") return;  // the paste event carries the text
    if (e.isComposing || e.keyCode === 229 || e.key === "Dead" || e.key === "Process") return;  // compositionend forwards it
    // History only on ⌘[ / ⌘] (never ⌥←/⌥→: those are word moves on macOS and belong to the page).
    if (down && e.metaKey && e.key === "[") { e.preventDefault(); nav("back"); return; }
    if (down && e.metaKey && e.key === "]") { e.preventDefault(); nav("forward"); return; }
    if (down && e.metaKey && k === "r") { e.preventDefault(); nav("reload"); return; }
    e.preventDefault();
    const o = getOverlay();
    if (o && o.kind !== "list") { if (down && e.key === "Escape") closeOverlay(); return; }  // the menu / picker owns the keyboard
    if (o?.kind === "list") {  // suggestions: arrows, Enter, Escape and Tab are ours; everything else types on and refreshes them
      if (["ArrowDown", "ArrowUp", "Enter", "Escape", "Tab"].includes(e.key)) {
        if (!down) return;
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { setOverlay({ ...o, hi: wrapIndex(o.hi, e.key === "ArrowDown" ? 1 : -1, o.opts.length) }); return; }
        if (e.key === "Enter") { pickSelect(o.opts[o.hi]?.v ?? null); return; }
        if (e.key === "Escape") { closeOverlay(); return; }
        setOverlay(null);  // Tab: moves focus in the page below, through the normal path
      }
    } else if (down && !e.metaKey && !e.ctrlKey && !e.altKey && (e.key === " " || e.key === "Enter" || e.key === "ArrowDown" || e.key === "ArrowUp")) {
      // On a focused closed <select> these open Chrome's (invisible) menu: open ours instead, else forward as usual.
      queue(async () => { if (!(await openOverlayAt(null))) sendKey(keyAction(e)); });
      if (keys) keys.value = "";
      return;
    }
    if (down && (e.metaKey || e.ctrlKey) && !e.altKey && (k === "c" || k === "x")) {
      // Copy / cut: the page's selection goes to YOUR clipboard too (read before the key: a cut removes it).
      queue(async () => { const text = await evalIn<string>(SEL_TEXT_PROBE); sendKey(keyAction(e)); if (text) copyText(text); });
      return;
    }
    queue(() => sendKey(keyAction(e)));
    if (keys) keys.value = "";
    if (down && e.key === "Enter") setTimeout(poll, 700);
    if (down && typesText(e)) scheduleSuggest();
  };
  const onCompose = (e: CompositionEvent) => {
    if (inCtl() && e.data) queue(() => cdp("Input.insertText", { text: e.data }));
    if (keys) keys.value = "";
  };
  // Whatever still reaches the textarea outside a composition (autocorrect) is dropped at once.
  const onKeysInput = (e: Event) => { if (keys && !(e as InputEvent).isComposing) keys.value = ""; };
  const onDocKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !inCtl() && inFull()) void handBack(true); };

  stage.addEventListener("mousemove", onMove);
  stage.addEventListener("mousedown", onDown);
  window.addEventListener("mouseup", onUp);
  stage.addEventListener("contextmenu", onCtx);
  stage.addEventListener("click", onClick);
  stage.addEventListener("paste", onPaste);
  stage.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("keydown", keyEv);
  stage.addEventListener("keyup", keyEv);
  keys?.addEventListener("compositionend", onCompose);
  keys?.addEventListener("input", onKeysInput);
  document.addEventListener("keydown", onDocKey);

  // After each poll: follow popups, keep the socket on the driven tab, keep the per-session switches in line with
  // whether you drive, and drop anything that only belonged to a take-over once it ends.
  let lastBots = getState().bots, lastFast = getState().fast, lastSel = getState().sel;
  const unsub = subscribeStore(() => {
    const s = getState();
    if (s.bots === lastBots && s.fast === lastFast && s.sel === lastSel) return;
    lastBots = s.bots; lastFast = s.fast; lastSel = s.sel;
    const b = cur(); if (b) followPopups(b);
    linkSync();
    syncDriving();
    if (!inCtl()) endDriving();
  });

  return () => {
    unsub();
    stage.removeEventListener("mousemove", onMove);
    stage.removeEventListener("mousedown", onDown);
    window.removeEventListener("mouseup", onUp);
    stage.removeEventListener("contextmenu", onCtx);
    stage.removeEventListener("click", onClick);
    stage.removeEventListener("paste", onPaste);
    stage.removeEventListener("wheel", onWheel);
    stage.removeEventListener("keydown", keyEv);
    stage.removeEventListener("keyup", keyEv);
    keys?.removeEventListener("compositionend", onCompose);
    keys?.removeEventListener("input", onKeysInput);
    document.removeEventListener("keydown", onDocKey);
    endDriving();
  };
}
