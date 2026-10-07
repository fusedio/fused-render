// THE ONE FORK every "here is a command — run it if there is somewhere to run
// it, otherwise copy it" affordance in the app makes: the explorer's "Open in
// Claude" row (useFileOps.ts) and header action (Preview.tsx), and the task
// peek's "Continue in terminal" (shell/TaskPeek.tsx). All three built this
// exact `if (canRunInTerminal()) openTerminal(...) else copyToClipboard(...)`
// shape independently; this is the shared version, so the day either half of
// it changes there is exactly one place to edit.
//
// `apps/claude/ui/Kebab.tsx`'s "Continue in terminal" is NOT a caller: it
// reports success and failure inline in the menu item's own label rather than
// as a toast, which is a different shape from the one this file gives —
// sharing this would mean threading a toast-vs-label choice through it for a
// single caller.
import { canRunInTerminal, openTerminal } from "./terminalDockStore";
import { copyPendingText, copyToClipboard } from "./clipboard";
import { notify } from "./notifications";

export async function runOrCopyInTerminal(
  /** The command, or a promise of one for a caller that has to ask the server
   *  for it. Pass the PROMISE in that case, not the awaited string: on the
   *  clipboard path the write has to start inside the click, before any
   *  await, or WebKit refuses it (see copyPendingText in clipboard.ts). A
   *  promise that rejects makes THIS reject with the same error, on both
   *  paths: a command that never arrived is the caller's to report, not a
   *  "Could not copy" toast. */
  command: string | Promise<string>,
  opts: {
    /** Where the drawer `cd`s to before typing/running `command`. */
    cwd?: string;
    /** Passed through to `openTerminal` — `false` types `command` at the
     *  prompt without running it. Omitted (the default) runs it immediately. */
    execute?: boolean;
    /** The string put on the clipboard when there is nowhere to run it.
     *  Defaults to `command` itself, but a caller whose `command` relies on
     *  `cwd` for its `cd` (a raw clipboard paste has no such mechanism) passes
     *  the self-contained version here instead — e.g. `"claude"` to run
     *  against `cwd`, `"cd '<cwd>' && claude"` to copy. */
    copyCommand?: string;
    /** Said on a successful clipboard fallback. Defaults to the sentence
     *  every one of these call sites already used. */
    copiedMessage?: string;
    /** Said on success when the command actually ran. Omitted by most
     *  callers, since the drawer opening in front of the reader IS the
     *  feedback and a toast on top of it would be a second one. */
    ranMessage?: string;
  } = {},
): Promise<boolean> {
  if (canRunInTerminal()) {
    openTerminal({
      cwd: opts.cwd,
      command: await command,
      ...(opts.execute === false ? { execute: false as const } : {}),
    });
    if (opts.ranMessage) notify({ title: opts.ranMessage, tone: "info" });
    return true;
  }
  const toCopy = opts.copyCommand ?? command;
  const ok = await (typeof toCopy === "string" ? copyToClipboard(toCopy) : copyPendingText(toCopy));
  notify({
    title: ok ? (opts.copiedMessage ?? "Command copied — paste it in your terminal") : "Could not copy the command",
    tone: ok ? "info" : "error",
  });
  return false;
}
