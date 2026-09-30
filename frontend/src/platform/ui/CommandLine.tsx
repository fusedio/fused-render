// One shared "here is a shell command" plate: the command in code styling,
// a Copy button, and — wherever the status-bar terminal exists to run it in
// (`canRunInTerminal()`) — a Run button beside it. Every command-to-copy
// surface in the app (the Claude health strip's install/update/PATH-fix
// commands, the trouble card's install box) renders through this instead of
// keeping its own copy button, so "here is a line to run" looks and behaves
// identically wherever the app offers one, and a Run button appears in every
// one of them the day the drawer becomes available.
import { useState } from "react";

import { copyToClipboard } from "@platform/lib/clipboard";
import { canRunInTerminal, openTerminal } from "@platform/lib/terminalDockStore";

const COPY_RESET_MS = 2000;

export function CommandLine({
  command,
  cwd,
  /** Defaults to true — Run sends the command immediately. Pass false for a
   * command a person should read before it executes (unused by today's
   * install/update/PATH-fix callers, which all want to run right away, but
   * kept so a future type-only caller does not need its own component). */
  execute = true,
  /** Called once Run has handed the command to the drawer — a caller with
   * its own "how do I know it worked" story (the health strip's re-check)
   * hangs it here instead of polling the drawer itself. */
  onRun,
}: {
  command: string;
  cwd?: string;
  execute?: boolean;
  onRun?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="update-badge-command">
      <code>{command}</code>
      {canRunInTerminal() && (
        <button
          type="button"
          className="update-badge-run"
          onClick={() => {
            openTerminal({ cwd, command, execute });
            onRun?.();
          }}
        >
          Run
        </button>
      )}
      <button
        type="button"
        className="update-badge-copy"
        onClick={async () => {
          const ok = await copyToClipboard(command);
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), COPY_RESET_MS);
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export default CommandLine;
