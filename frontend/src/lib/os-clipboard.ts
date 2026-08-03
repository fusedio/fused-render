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
import {
  beginOsObservation,
  commitOsToken,
  getClipboardEpoch,
  getLastSeenOsToken,
  setClipboard,
} from "./fs-clipboard";

export async function reconcileOsClipboard(): Promise<void> {
  // Captured before the read, checked after it. The read is a round-trip to the
  // server, and the user can copy or cut in the app while it is in flight; what
  // comes back then describes a moment that has passed, and adopting it would
  // silently discard the gesture they just made.
  const epoch = getClipboardEpoch();
  // Taken before the read, for the same reason the epoch is: this observation
  // describes the clipboard as of NOW, and a mirror-write issued earlier but
  // delivered later must not overwrite what we are about to record with its
  // older news. See `beginOsObservation`.
  const seq = beginOsObservation();
  let os;
  try {
    os = await readOsClipboard();
  } catch {
    // No bridge, a dead request, a sandboxed clipboard — all "we don't know",
    // and none of them should disturb what's already in the app.
    return;
  }

  if (!os.supported || os.paths.length === 0) return;
  // Superseded mid-read (see above). Neither the paths nor the token are
  // adopted: recording the token would make the NEXT reconcile treat this
  // clipboard as already seen and skip it for good.
  if (getClipboardEpoch() !== epoch) return;
  // The token tracks what we last SAW, not what we last wrote. An unchanged
  // clipboard means there is nothing new to adopt — crucially, that leaves a
  // pending in-app cut alone instead of overwriting it with a stale copy on
  // every focus change. The cost is that re-copying the identical selection
  // in the file manager isn't seen as a new event, which is harmless since
  // adopting the same paths is idempotent.
  if (os.token === getLastSeenOsToken()) return;

  commitOsToken(seq, os.token);
  // Always a copy. No platform exposes a reliable cut-vs-copy flag on read,
  // and honouring one would mean deleting the user's source files on a guess.
  setClipboard({ paths: os.paths, op: "copy" }, false);
}
