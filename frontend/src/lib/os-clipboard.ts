// Adopting the system clipboard: files copied in Finder / Explorer / Nautilus
// / Dolphin become the app's clipboard, so Paste in the explorer drops them
// into the current directory.
//
// Deliberately a second writer of the EXISTING module clipboard store rather
// than a parallel "external clipboard" state: that keeps all four Paste sites,
// the `disabled: !clipboard` checks and the cut-dimming working untouched.
//
// Run on return to the app (App.tsx wires it to useRefreshOnReturn), not on a
// poll — coming back to the window is the only moment the clipboard can have
// changed from the user's point of view. A copy made while the app is already
// focused is therefore missed until the next focus change.
import { readOsClipboard } from "./api";
import { getLastSeenOsToken, setClipboard, setLastSeenOsToken } from "./fs-clipboard";

export async function reconcileOsClipboard(): Promise<void> {
  let os;
  try {
    os = await readOsClipboard();
  } catch {
    // No bridge, a dead request, a sandboxed clipboard — all "we don't know",
    // and none of them should disturb what's already in the app.
    return;
  }

  if (!os.supported || os.paths.length === 0) return;
  // The token tracks what we last SAW, not what we last wrote. An unchanged
  // clipboard means there is nothing new to adopt — crucially, that leaves a
  // pending in-app cut alone instead of overwriting it with a stale copy on
  // every focus change. The cost is that re-copying the identical selection
  // in the file manager isn't seen as a new event, which is harmless since
  // adopting the same paths is idempotent.
  if (os.token === getLastSeenOsToken()) return;

  setLastSeenOsToken(os.token);
  // Always a copy. No platform exposes a reliable cut-vs-copy flag on read,
  // and honouring one would mean deleting the user's source files on a guess.
  setClipboard({ paths: os.paths, op: "copy" }, false);
}
