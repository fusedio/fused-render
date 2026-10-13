// The floating recording pill (recording-pill.html → here). The native panel (fused_render/recording_pill_panel.py)
// hosts this page in a non-activating web view and pushes window.pillSet(list) (every active recording, newest
// last) / window.pillHidden(); the page polls GET /api/capture/{id}/state for each one's clock, paused flag and
// level, and talks back over window.webkit.messageHandlers.recpill ({type: ready|stop|open|gone|drag|size}).
// Nothing here needs a newer WebKit than macOS 13 ships.
import botHomeImg from "@assets/fusedbot-icon-1024.png";
import renderHomeImg from "@assets/fusedrender-icon-1024.png";
import { bars, combineStates, formatElapsed, iconFor, shapeLevel, smoothLevel, visibleApps, type PillInfo, type PillState } from "./lib";

declare global {
  interface Window {
    pillSet?: (list: PillInfo[]) => void;
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
const appsEl = document.getElementById("apps") as HTMLElement;
const meterEl = document.getElementById("meter") as HTMLElement;
const meter = Array.from(meterEl.querySelectorAll<HTMLElement>("i"));
const stopBtn = document.getElementById("stop") as HTMLButtonElement;

const POLL_MS = 100;
let infos: PillInfo[] = [];
const states = new Map<string, PillState>();
let timer: number | undefined;
let polling = false;
let displayed = 0;
const BAR_MAX_PX = 24;

function appEl(i: PillInfo): HTMLElement {
  const art = iconFor(i);
  const el = document.createElement("div");
  el.className = "app" + (art.kind === "home" ? " home" : "");
  el.title = i.name;
  el.dataset.id = i.id;
  el.style.setProperty("--home-scale", art.scale);
  const wrap = document.createElement("div");
  wrap.className = "art";
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  img.src = art.src ?? (i.flavor === "bot" ? botHomeImg : renderHomeImg);
  // An app icon that fails to load falls back to the flavor's card rather than a broken glyph.
  if (art.kind === "app") img.onerror = () => { el.replaceWith(appEl({ ...i, icon: null })); applyPaused(); };
  wrap.appendChild(img);
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.title = "Stop " + i.name;
  badge.appendChild(document.createElement("i"));
  badge.addEventListener("click", (e) => { e.stopPropagation(); post({ type: "stop", id: i.id }); });
  el.append(wrap, badge);
  el.addEventListener("click", () => post({ type: "open", id: i.id, app: i.app }));
  return el;
}

function applyPaused() {
  appsEl.querySelectorAll<HTMLElement>(".app").forEach((el) => {
    el.classList.toggle("paused", !!states.get(el.dataset.id ?? "")?.paused);
  });
}

function reportSize() {
  if (!pill.hidden) post({ type: "size", height: pill.offsetHeight });
}

function render() {
  const { shown, extra } = visibleApps(infos);
  appsEl.replaceChildren(...shown.map(appEl));
  if (extra > 0) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.textContent = "+" + extra;
    chip.title = infos.slice(0, extra).map((i) => i.name).join(", ");
    appsEl.appendChild(chip);
  }
  pill.classList.toggle("multi", infos.length > 1);
  stopBtn.title = infos.length > 1 ? "Stop all" : "Stop and save";
  applyPaused();
  reportSize();
}

function drawMeter() {
  const c = combineStates(infos.map((i) => states.get(i.id)));
  displayed = smoothLevel(shapeLevel(c.level), displayed);
  pill.classList.toggle("paused", c.paused);
  meterEl.title = "Recording " + formatElapsed(c.seconds);
  meterEl.classList.toggle("live", !c.paused && displayed > 0.12);
  bars(displayed, c.paused).forEach((w, k) => { if (meter[k]) meter[k].style.width = Math.round(w * BAR_MAX_PX) + "px"; });
}

async function poll() {
  if (!infos.length || polling) return;
  polling = true;
  const ids = infos.map((i) => i.id);
  try {
    const gone: string[] = [];
    await Promise.all(ids.map(async (id) => {
      try {
        const r = await fetch("/api/capture/" + encodeURIComponent(id) + "/state", { cache: "no-store" });
        if (r.status === 404) gone.push(id);
        else if (r.ok) states.set(id, (await r.json()) as PillState);
      } catch { /* the server is restarting: keep the last frame */ }
    }));
    if (!infos.length) return;
    if (gone.length) {
      for (const id of gone) { post({ type: "gone", id }); states.delete(id); }
      infos = infos.filter((i) => !gone.includes(i.id));
      if (!infos.length) { hideSelf(); return; }
      render();
    }
    drawMeter();
  } finally { polling = false; }
}

function startPolling() {
  if (timer === undefined) timer = window.setInterval(poll, POLL_MS);
  void poll();
}
function hideSelf() {
  if (timer !== undefined) { window.clearInterval(timer); timer = undefined; }
  infos = [];
  states.clear();
  pill.hidden = true;
}

window.pillSet = (list: PillInfo[]) => {
  if (!list.length) { hideSelf(); return; }
  const keep = new Set(list.map((i) => i.id));
  for (const id of Array.from(states.keys())) if (!keep.has(id)) states.delete(id);
  infos = list.slice();
  pill.hidden = false;
  render();
  drawMeter();
  startPolling();
};
window.pillShow = (i: PillInfo) => window.pillSet?.([i]);
window.pillHidden = hideSelf;

stopBtn.addEventListener("click", () => { for (const i of infos) post({ type: "stop", id: i.id }); });

// Dragging the pill moves the native window: report the pointer's screen delta from where the press began.
let drag: { x: number; y: number } | null = null;
pill.addEventListener("pointerdown", (e) => {
  if ((e.target as HTMLElement).closest("button, .app, .chip")) return;
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
  if (cid) {
    window.pillSet?.(cid.split(",").filter(Boolean).map((id) => (
      { id, name: id, app: "", icon: null, flavor: "render" as const, canPause: true, mode: "audio" })));
  }
}
