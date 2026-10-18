// The boot's one wait for server-held UI state (D1384): the layout document
// (ui-layout.ts) plus whatever the entry adds — main.tsx passes the
// notifications panel's seen-state hydrator, which lives in shell/ and so
// cannot be imported from here (platform imports only platform and assets).
//
// WHY THE FIRST RENDER WAITS FOR IT. These stores used to be localStorage,
// which is synchronous: the sidebar painted at its dragged width, the Tasks
// page opened on the Board, from the very first frame. Server-held state that
// arrived a tick after the render would be a flash of the default layout on
// every load — the sidebar snapping from 232 to its real width, a folder
// re-sorting under the cursor. They moved to the server at all because the
// desktop app's port walk (1777..1787, then an ephemeral port) hands every
// relaunch a new origin with empty storage; a flash on every launch would trade
// one visible bug for another.
//
// BOUNDED, because a wait that cannot end is a blank window: past
// `UI_STATE_TIMEOUT_MS` this resolves anyway and the stores run on their
// defaults (ui-layout.ts adopts the server's copy whenever it does land, and
// notifies). Never rejects — every hydrator swallows its own failure, and this
// swallows any that does not.
import { hydrateUiLayout } from "@platform/lib/ui-layout";

/** Long enough for a cold local server (/api/config's own bound argues the
 *  measurements), short enough that a wedged one costs a beat, not the app. */
export const UI_STATE_TIMEOUT_MS = 2000;

export function hydrateUiState(extra: Array<() => Promise<unknown>> = []): Promise<void> {
  const all = Promise.all(
    [hydrateUiLayout, ...extra].map((fn) => {
      try {
        return Promise.resolve(fn()).catch(() => undefined);
      } catch {
        return undefined;
      }
    })
  ).then(() => undefined);
  const bound = new Promise<void>((resolve) => setTimeout(resolve, UI_STATE_TIMEOUT_MS));
  return Promise.race([all, bound]);
}
