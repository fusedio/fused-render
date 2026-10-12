// The floating recording pill (recording-pill.html → here). The native panel (fused_render/recording_pill_panel.py)
// hosts this page in a non-activating web view and pushes window.pillShow(info) / window.pillHidden(); the page
// polls GET /api/capture/{id}/state for the clock, paused flag and level, and talks back over
// window.webkit.messageHandlers.recpill ({type: ready|pause|resume|stop|open|gone|drag}). Nothing here needs a
// newer WebKit than macOS 13 ships.
import botHomeImg from "@assets/fusedbot-icon-1024.png";
import renderHomeImg from "@assets/fusedrender-icon-1024.png";
import { bars, formatElapsed, iconFor, resolvePaused, shapeLevel, smoothLevel, type PendingPause, type PillInfo, type PillState } from "./lib";

declare global {
  interface Window {
    pillShow?: (info: PillInfo) => void;
    pillHidden?: () => void;
  }
}
type Bridge = { postMessage(m: Record<string, unknown>): void };
const bridge = (): Bridge | undefined =>
  (window as unknown as { webkit?: { messageHandlers?: { recpill?: Bridge } } }).webkit?.messageHandlers?.recpill;
const post = (m: Record<string, unknown>) => { try { bridge()?.postMessage(m); } catch { /* panel gone */ } };

const INAPP = navigator.userAgent.includes("FusedRender/");
if (!INAPP) document.body.classList.add("dev");

const pill = document.getElementById("pill") as HTMLElement;
const iconEl = document.getElementById("icon") as HTMLElement;
const meterEl = document.getElementById("meter") as HTMLElement;
const meter = Array.from(meterEl.querySelectorAll<HTMLElement>("i"));
const pauseBtn = document.getElementById("pause") as HTMLButtonElement;
const stopBtn = document.getElementById("stop") as HTMLButtonElement;

const POLL_MS = 100;
let info: PillInfo | null = null;
let paused = false;
let pending: PendingPause | null = null;
let timer: number | undefined;
let polling = false;
let displayed = 0;
const BAR_MAX_PX = 26;

function drawIcon(i: PillInfo) {
  const art = iconFor(i);
  iconEl.replaceChildren();
  iconEl.classList.toggle("home", art.kind === "home");
  iconEl.style.setProperty("--home-scale", art.scale);
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  img.src = art.src ?? (i.flavor === "bot" ? botHomeImg : renderHomeImg);
  // An app icon that fails to load falls back to the flavor's card rather than a broken glyph.
  if (art.kind === "app") img.onerror = () => { drawIcon({ ...i, icon: null }); };
  iconEl.appendChild(img);
}

function draw(s: PillState) {
  const r = resolvePaused(s.paused, pending, Date.now());
  paused = r.paused;
  if (pending && !r.pending) pauseBtn.disabled = false;
  pending = r.pending;
  pill.classList.toggle("paused", paused);
  pauseBtn.title = paused ? "Resume" : "Pause";
  meterEl.title = "Recording " + formatElapsed(s.seconds ?? 0);
  displayed = smoothLevel(shapeLevel(s.level ?? 0), displayed);
  bars(displayed, paused).forEach((w, k) => { if (meter[k]) meter[k].style.width = Math.round(w * BAR_MAX_PX) + "px"; });
}

async function poll() {
  if (!info || polling) return;
  polling = true;
  const mine = info.id;
  try {
    const r = await fetch("/api/capture/" + encodeURIComponent(mine) + "/state", { cache: "no-store" });
    if (info?.id !== mine) return;
    if (r.status === 404) { post({ type: "gone", id: mine }); hideSelf(); return; }
    if (r.ok) draw((await r.json()) as PillState);
  } catch { /* the server is restarting: keep the last frame */ }
  finally { polling = false; }
}

function startPolling() {
  if (timer === undefined) timer = window.setInterval(poll, POLL_MS);
  void poll();
}
function hideSelf() {
  if (timer !== undefined) { window.clearInterval(timer); timer = undefined; }
  info = null;
  pill.hidden = true;
}

window.pillShow = (i: PillInfo) => {
  info = i;
  paused = false;
  pending = null;
  displayed = 0;
  pauseBtn.disabled = false;
  drawIcon(i);
  pauseBtn.hidden = !i.canPause;
  pill.classList.remove("paused");
  draw({ seconds: 0, level: 0 });
  pill.hidden = false;
  startPolling();
};
window.pillHidden = hideSelf;

pauseBtn.addEventListener("click", () => {
  if (!info || pending) return;
  const next = !paused;
  // Optimistic: polls keep this value until the server agrees or the 2 s deadline passes.
  pending = { want: next, until: Date.now() + 2000 };
  pauseBtn.disabled = true;
  paused = next;
  pill.classList.toggle("paused", next);
  post({ type: next ? "pause" : "resume", id: info.id });
});
stopBtn.addEventListener("click", () => { if (info) post({ type: "stop", id: info.id }); });
iconEl.addEventListener("click", () => { if (info) post({ type: "open", id: info.id, app: info.app }); });

// Dragging the pill moves the native window: report the pointer's screen delta from where the press began.
let drag: { x: number; y: number } | null = null;
pill.addEventListener("pointerdown", (e) => {
  if ((e.target as HTMLElement).closest("button, #icon")) return;
  drag = { x: e.screenX, y: e.screenY };
  pill.setPointerCapture(e.pointerId);
  post({ type: "drag", phase: "start", dx: 0, dy: 0 });
});
pill.addEventListener("pointermove", (e) => {
  if (!drag) return;
  post({ type: "drag", phase: "move", dx: e.screenX - drag.x, dy: e.screenY - drag.y });
});
const endDrag = (e: PointerEvent) => {
  if (!drag) return;
  post({ type: "drag", phase: "end", dx: e.screenX - drag.x, dy: e.screenY - drag.y });
  drag = null;
};
pill.addEventListener("pointerup", endDrag);
pill.addEventListener("pointercancel", endDrag);

// Tell the panel the page is listening (it pushes the current recording then), and for a plain browser tab
// show ?cid=<id> so the pill can be designed against a live recording.
post({ type: "ready" });
if (!INAPP) {
  const cid = new URLSearchParams(location.search).get("cid");
  if (cid) window.pillShow({ id: cid, name: cid, app: "", icon: null, flavor: "render", canPause: true, mode: "audio" });
}
