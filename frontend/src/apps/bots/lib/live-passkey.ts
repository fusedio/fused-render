// Passkeys while the live view is open. Headless Chrome has nowhere to draw its passkey picker (or the macOS sheet behind
// it): a page's navigator.credentials.get/create never settles, the page spins forever (measured, Chrome 155). The picker
// is browser UI, so no screencast can carry it either; only the real window (popout) can. So while the browser is
// headless, this socket wraps the page's passkey calls (SHIM), and a modal one asks here instead of reaching Chrome:
// "Open real window" relaunches the same profile headed on this page (the existing popout; the bot pauses and you take
// over), "Not now" rejects it with NotAllowedError so the page falls back ("Try another way"). Conditional calls
// (passkey autofill, which sign-in pages such as webauthn.io start on load) and non-passkey credentials pass through
// untouched; "immediate" ones are told no at once.
//
// Per CDP session, like live-page.ts: the binding and the document script are re-added on every socket (onOpen) and die
// with it. A request lives in the page, not the socket: a re-link asks the page for the ones still waiting (pending()).
// Not covered: an out-of-process cross-origin iframe (its own target), and a page loaded while the live view was closed.
import { askChoice } from "../dialogs/ask";
import { act, cur } from "../state/store";
import { api } from "./api";
import { cdp, evalIn, onEvent, onOpen, onReset } from "./cdp";

const BINDING = "__fusedPasskey";

// Runs in every frame before the page's own scripts (and at once in the frames already loaded). A modal request is never
// handed to headless Chrome (it would hang there anyway): it reports {id, kind, rp} through the binding and waits;
// {id, done} when the page itself gave up (its AbortSignal, its publicKey.timeout). The host settles it with
// window[Symbol.for("fused.passkey")].settle(id), or the relaunch ends the page.
const SHIM = `(() => {
  const K = Symbol.for("fused.passkey"), C = window.CredentialsContainer;
  if (window[K] || !C || !navigator.credentials) return;
  const proto = C.prototype, real = { get: proto.get, create: proto.create }, asks = new Map();
  let n = 0, timer = 0;  // ids are unique across frames: the host keys its dialogs by id alone
  const bound = () => typeof window.${BINDING} === "function";
  const report = (o) => { try { window.${BINDING}(JSON.stringify(o)); return true; } catch (e) { return false; } };
  // The binding is re-added after each navigation (it only outlives one with Runtime.enable), so a request made while
  // the page loads can come before it: it waits here and is reported once the binding shows up.
  const flush = () => {
    for (const [id, a] of asks) if (!a.told) a.told = report({ id, kind: a.kind, rp: a.rp });
    const left = [...asks.values()].some((a) => !a.told);
    if (left && !timer) timer = setInterval(flush, 250);
    if (!left && timer) { clearInterval(timer); timer = 0; }
  };
  const denied = () => new DOMException("The operation either timed out or was not allowed.", "NotAllowedError");
  const ask = (kind, o) => {
    const pk = o && o.publicKey;
    if (!pk || o.mediation === "conditional") return null;
    // "immediate" asks for a credential only if one is there without any UI; this browser can show none: "nothing here".
    if (o.mediation === "immediate") return Promise.reject(denied());
    const sig = o.signal, aborted = () => sig.reason || new DOMException("The operation was aborted.", "AbortError");
    if (sig && sig.aborted) return Promise.reject(aborted());
    const id = (++n) + "-" + Math.random().toString(36).slice(2, 10), rp = (kind === "get" ? pk.rpId : pk.rp && pk.rp.id) || location.hostname;
    return new Promise((resolve, reject) => {
      let t = 0;
      const onAbort = () => done(aborted(), true);
      const done = (err, own) => {
        const a = asks.get(id); if (!a) return;
        asks.delete(id); clearTimeout(t);
        if (sig) sig.removeEventListener("abort", onAbort);
        if (own && a.told && bound()) report({ id, done: true });
        reject(err);
      };
      if (sig) sig.addEventListener("abort", onAbort);
      if (pk.timeout > 0) t = setTimeout(() => done(denied(), true), pk.timeout);
      asks.set(id, { kind, rp, done, told: false });
      flush();
    });
  };
  for (const kind of ["get", "create"]) {
    const d = Object.getOwnPropertyDescriptor(proto, kind);
    if (!d || typeof d.value !== "function") continue;
    const fn = { [kind](o) { return ask(kind, o) || real[kind].call(this, o); } }[kind];
    Object.defineProperty(proto, kind, { ...d, value: fn });
  }
  Object.defineProperty(window, K, { value: {
    settle: (id) => { const a = asks.get(id); if (a) a.done(denied(), false); },
    pending: () => [...asks].map(([id, a]) => { a.told = true; return { id, kind: a.kind, rp: a.rp }; }),  // the host asks now: no second report
  } });
})()`;

type Ask = { id: string; kind?: string; rp?: string; done?: boolean };
/** The dialogs up on this socket, keyed by request id (a re-link's pending() and a late report may name the same one); dismissed with the socket. */
const asking = new Map<string, () => void>();

// Without Runtime.enable (which would stream every console message over this socket) a binding does not outlive the
// document it was added to (measured, Chrome 155): re-add it on every navigation, top frame and iframes alike.
let shimmed = false;
onOpen.push(() => {
  // A popped-out (headed) browser is real Chrome: its passkey picker works, so its pages are left alone.
  shimmed = !cur()?.browser?.headed;
  if (!shimmed) return;
  cdp("Runtime.addBinding", { name: BINDING });
  cdp("Page.addScriptToEvaluateOnNewDocument", { source: SHIM, runImmediately: true });
  // Requests still waiting from an earlier link (another bot was selected, the view was closed and reopened).
  void evalIn<Ask[]>(`window[Symbol.for("fused.passkey")]?.pending() || []`).then((rows) => {
    for (const a of rows || []) void ask(a, undefined);
  });
});
onEvent("Page.frameNavigated", () => { if (shimmed) cdp("Runtime.addBinding", { name: BINDING }); });
onReset.push(() => { shimmed = false; const all = [...asking.values()]; asking.clear(); for (const dismiss of all) dismiss(); });

onEvent("Runtime.bindingCalled", (p) => {
  if (p.name !== BINDING) return;
  let a: Ask;
  try { a = JSON.parse(String(p.payload)); } catch { return; }
  const ctx = typeof p.executionContextId === "number" ? p.executionContextId : undefined;
  if (a.done) { const dismiss = asking.get(a.id); asking.delete(a.id); dismiss?.(); return; }
  void ask(a, ctx);
});

/** Reject one request in the page (in its own frame's context when it came from an iframe). */
function settle(ctx: number | undefined, id: string): void {
  cdp("Runtime.evaluate", { expression: `window[Symbol.for("fused.passkey")]?.settle(${JSON.stringify(id)})`, ...(ctx !== undefined ? { contextId: ctx } : {}) });
}

async function ask(a: Ask, ctx: number | undefined): Promise<void> {
  const b = cur(), k = a.id;
  if (!b || typeof k !== "string" || asking.has(k)) return;
  const rp = a.rp || "This page", save = a.kind === "create";
  const { done, dismiss } = askChoice(
    save ? `${rp} wants to save a passkey` : `${rp} wants a passkey`,
    "Passkeys only open in a real Chrome window. Chrome reopens on this page in a few seconds; click the passkey option there again."
      + (b.control ? "" : ` ${b.name} pauses and you take over.`),
    "Open real window", "Not now");
  asking.set(k, dismiss);
  const ok = await done;
  if (asking.get(k) !== dismiss) return;  // dismissed: the page gave up, or the socket went away
  asking.delete(k);
  // The relaunch ends this page, request and all; a popout that was refused (another bot mid-task on a shared browser,
  // its banner says so) leaves the page to fall back like Not now.
  if (ok && await act(() => api.popout(b.id))) return;
  settle(ctx, a.id);
}
