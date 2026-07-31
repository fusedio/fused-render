// Opening a deployed app (SPEC §34) is requested from several places and rendered in ONE:
// the path bar (a pasted https:// link) and the Apps page both fire this event, and
// `CloneAppHost` — mounted at the shell, beside NotificationHost — owns the modal.
//
// An event rather than lifted state because the callers are unrelated and far apart: the
// path bar's URL handler is a module-level function with no component context at all, and
// the Apps page renders without the sidebar. Threading a callback from the shell down to
// both would couple three files to a flow none of them owns. This is the same
// window-event seam `lib/account.ts` uses for the signed-in dot.
const CLONE_APP_EVENT = "fused-render:open-deployed-app";

/** Ask the shell to open the "Open a deployed app" flow, optionally pre-filled.
 *
 * `src` is a URL the user has already supplied (a pasted link); the confirm step still
 * requires their explicit click, because arriving with a URL is not consent to write files.
 */
export function requestCloneApp(src = ""): void {
  window.dispatchEvent(new CustomEvent(CLONE_APP_EVENT, { detail: src }));
}

/** Subscribe to open requests. Returns an unsubscribe, like every other listener here. */
export function onCloneAppRequest(handler: (src: string) => void): () => void {
  const listener = (e: Event) => {
    const detail = (e as CustomEvent).detail;
    handler(typeof detail === "string" ? detail : "");
  };
  window.addEventListener(CLONE_APP_EVENT, listener);
  return () => window.removeEventListener(CLONE_APP_EVENT, listener);
}
