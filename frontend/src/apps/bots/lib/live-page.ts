// What the page asks for and headless Chrome cannot show: JavaScript dialogs, the file chooser and HTTP auth
// challenges, each turned into a dialog of ours while you drive. Also the per-session switches that only make sense
// while you drive (`syncDriving`): the Fetch domain for auth (it pauses every request, so never while the bot works),
// and interception of the file chooser and of drags (a bot's own file input or drag must reach Chrome untouched).
// Everything here is per CDP session: re-applied on every socket (onOpen) and forgotten with it (onReset).
import { pickFile } from "@platform/lib/api";
import { askAuth, askConfirm, askPrompt } from "../dialogs/ask";
import { showToast } from "../state/store";
import { cdp, focusCtl, inCtl, linked, onEvent, onOpen, onReset } from "./cdp";
import { authKey } from "./live";

const toast = (text: string) => showToast({ text, ts: Date.now() / 1000 });

let fetchOn = false, interceptsOn = false;
const authSeen = new Set<string>();
/** Bring the session's switches in line with whether you drive. Cheap; called on open and on every store change. */
export function syncDriving(): void {
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
onReset.push(() => { fetchOn = false; interceptsOn = false; authSeen.clear(); });

// alert / confirm / prompt / beforeunload. While you drive they are real dialogs (confirm and beforeunload can be refused,
// prompt has its text field). While the bot drives they are the bot's: its own run accepts them and reads the message
// (browser.py _settle_dialog), so here only a toast says what happened.
onEvent("Page.javascriptDialogOpening", (p) => { void pageDialog(p as { type: string; message?: string; defaultPrompt?: string }); });
async function pageDialog(p: { type: string; message?: string; defaultPrompt?: string }): Promise<void> {
  const msg = p.message || "";
  if (!inCtl()) { toast(`${p.type}: ${msg}`); return; }
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
// continued at once; only the challenge gets a dialog. A second challenge for the same origin+realm while the first
// answer is fresh means the password was wrong: cancelled, never looped. Cancel also clears the key, so a reload may retry.
type Challenge = { requestId: string; request?: { url?: string }; authChallenge?: { source?: string; origin?: string; realm?: string } };
onEvent("Fetch.requestPaused", (p) => cdp("Fetch.continueRequest", { requestId: p.requestId }));
onEvent("Fetch.authRequired", (p) => { void authChallenge(p as Challenge); });
async function authChallenge(p: Challenge): Promise<void> {
  const ch = p.authChallenge || {}, key = authKey(ch);
  if (authSeen.has(key) || !inCtl()) { cdp("Fetch.continueWithAuth", { requestId: p.requestId, authChallengeResponse: { response: "CancelAuth" } }); return; }
  authSeen.add(key);
  const who = ch.source === "Proxy" ? `The proxy ${ch.origin || ""}` : ch.origin || p.request?.url || "The site";
  const creds = await askAuth("Sign in required", `${who} asks for a user name and password${ch.realm ? ` (${ch.realm})` : ""}.`);
  cdp("Fetch.continueWithAuth", { requestId: p.requestId, authChallengeResponse: creds
    ? { response: "ProvideCredentials", username: creds.user, password: creds.pass } : { response: "CancelAuth" } });
  if (creds) window.setTimeout(() => authSeen.delete(key), 15000); else authSeen.delete(key);
  focusCtl();
}
