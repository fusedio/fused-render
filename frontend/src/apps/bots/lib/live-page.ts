// What the page asks for and headless Chrome cannot show: JavaScript dialogs, the file chooser and HTTP auth
// challenges, each turned into a dialog of ours while you drive. Also the per-session switches that only make sense
// while you drive (`syncDriving`): the Fetch domain for auth (it pauses every request, so never while the bot works),
// and interception of the file chooser and of drags (a bot's own file input or drag must reach Chrome untouched).
// Everything here is per CDP session: re-applied on every socket (onOpen) and forgotten with it (onReset).
import { pickFile } from "@platform/lib/api";
import { askAuth, askConfirm, askPrompt } from "../dialogs/ask";
import { cur, showToast } from "../state/store";
import { api } from "./api";
import { cdp, focusCtl, inCtl, inFull, linked, onEvent, onOpen, onReset } from "./cdp";
import { authKey } from "./live";

const toast = (text: string) => showToast({ text, ts: Date.now() / 1000 });

let fetchOn = false, interceptsOn = false;
const authSeen = new Set<string>();

// Viewport fit. The bot's Chrome window follows the stage's size (browser.py set_viewport resizes the window; the bot
// addresses elements by ref and scales its screenshots, so any size suits it). Sent while the live view is open, whoever
// drives, so the frame fills the stage; the size sticks for the bot afterwards. Per bot: the last size sent is remembered
// so a poll or a store change does not repeat it.
let fitted: { id: string; size: string } | null = null;
export function fitViewport(): void {
  const b = cur(); const stage = document.getElementById("stage");
  if (!b || !stage || !inFull() || !stage.clientWidth || !stage.clientHeight) return;
  const size = `${Math.round(stage.clientWidth)}x${Math.round(stage.clientHeight)}`;
  if (fitted && fitted.id === b.id && fitted.size === size) return;
  fitted = { id: b.id, size };
  const [w, h] = size.split("x").map(Number);
  void api.viewport(b.id, w, h).catch(() => { fitted = null; });
}
let fitTimer: ReturnType<typeof setTimeout> | null = null;
/** The stage was resized (gutter drag, window, Stage opening): refit, debounced so a drag sends one resize at the end. */
export function fitViewportSoon(): void {
  if (fitTimer) clearTimeout(fitTimer);
  fitTimer = setTimeout(() => { fitTimer = null; fitViewport(); }, 200);
}

/** Bring the session's switches in line with whether you drive. Cheap; called on open and on every store change. */
export function syncDriving(): void {
  fitViewportSoon();
  if (!linked()) return;
  const want = inCtl();
  if (want !== fetchOn) {
    fetchOn = want;
    if (want) cdp("Fetch.enable", { handleAuthRequests: true, patterns: [{ urlPattern: "*" }] });
    else { cdp("Fetch.disable"); authSeen.clear(); }
  }
  if (want !== interceptsOn) {
    interceptsOn = want;
    cdp("Page.setInterceptFileChooserDialog", { enabled: want });
    cdp("Input.setInterceptDrags", { enabled: want });
  }
}
onOpen.push(syncDriving);
onReset.push(() => { fetchOn = false; interceptsOn = false; authSeen.clear(); authAsking.clear(); });

// alert / confirm / prompt / beforeunload. While you drive they are real dialogs (confirm and beforeunload can be refused,
// prompt has its text field). While you only watch they are accepted at once, as the bot's own run would (a dialog left
// open blocks the page, and the bot's next action still reports it: browser.py _settle_dialog), with a toast here.
onEvent("Page.javascriptDialogOpening", (p) => { void pageDialog(p as { type: string; message?: string; defaultPrompt?: string }); });
async function pageDialog(p: { type: string; message?: string; defaultPrompt?: string }): Promise<void> {
  const msg = p.message || "";
  if (!inCtl()) { toast(`${p.type}: ${msg}`); cdp("Page.handleJavaScriptDialog", { accept: true, promptText: p.defaultPrompt || "" }); return; }
  if (p.type === "alert") { toast(msg); cdp("Page.handleJavaScriptDialog", { accept: true }); return; }
  if (p.type === "prompt") {
    const v = await askPrompt("The page asks", msg, p.defaultPrompt || "");
    cdp("Page.handleJavaScriptDialog", { accept: v !== null, promptText: v ?? "" });
  } else {
    const ok = await askConfirm(p.type === "beforeunload" ? "Leave this page?" : "The page asks", msg, p.type === "beforeunload" ? "Leave" : "OK", false);
    cdp("Page.handleJavaScriptDialog", { accept: ok });
  }
  focusCtl();  // the dialog took the keyboard; typing continues on the page
}

// <input type=file> clicked while you drive: the OS picker runs in the server process, the chosen path lands on the input.
onEvent("Page.fileChooserOpened", (p) => { void fileChooser(p as { backendNodeId?: number }); });
async function fileChooser(p: { backendNodeId?: number }): Promise<void> {
  if (!p.backendNodeId || !inCtl()) return;
  let path: string | null = null;
  try { path = await pickFile({ title: "Choose a file for the page" }); } catch { path = null; }
  cdp("DOM.setFileInputFiles", { files: path ? [path] : [], backendNodeId: p.backendNodeId });
  focusCtl();
}

// HTTP auth (401/407). With the Fetch domain on, Chrome asks us instead of failing the request. Every paused request is
// continued at once; only the challenge gets a dialog. Parallel challenges for one origin+realm (a page and its images)
// share that dialog's answer; a challenge arriving right after an answer for the same realm is the wrong-password loop
// and is cancelled. Cancel leaves nothing behind, so a reload asks again.
type Challenge = { requestId: string; request?: { url?: string }; authChallenge?: { source?: string; origin?: string; realm?: string } };
type Creds = { user: string; pass: string } | null;
const authAsking = new Map<string, Promise<Creds>>();  // a dialog up for this realm
onEvent("Fetch.requestPaused", (p) => cdp("Fetch.continueRequest", { requestId: p.requestId }));
onEvent("Fetch.authRequired", (p) => { void authChallenge(p as Challenge); });
async function authChallenge(p: Challenge): Promise<void> {
  const ch = p.authChallenge || {}, key = authKey(ch);
  const answer = (creds: Creds) => cdp("Fetch.continueWithAuth", { requestId: p.requestId, authChallengeResponse: creds
    ? { response: "ProvideCredentials", username: creds.user, password: creds.pass } : { response: "CancelAuth" } });
  if (authSeen.has(key) || !inCtl()) { answer(null); return; }
  let ask = authAsking.get(key);
  if (!ask) {
    const who = ch.source === "Proxy" ? `The proxy ${ch.origin || ""}` : ch.origin || p.request?.url || "The site";
    ask = askAuth("Sign in required", `${who} asks for a user name and password${ch.realm ? ` (${ch.realm})` : ""}.`).then((creds) => {
      authAsking.delete(key);
      if (creds) { authSeen.add(key); window.setTimeout(() => authSeen.delete(key), 15000); }
      focusCtl();
      return creds;
    });
    authAsking.set(key, ask);
  }
  answer(await ask);
}
