// One shared "here is a shell command" plate: the command in code styling,
// a Copy button, and — wherever the status-bar terminal exists to run it in
// (`useCanRunInTerminal()`) — a Run button beside it. Every command-to-copy
// surface in the app (the Claude health strip's install/update/PATH-fix
// commands, the trouble card's install box) renders through this instead of
// keeping its own copy button, so "here is a line to run" looks and behaves
// identically wherever the app offers one.
import { useState } from "react";

import { copyToClipboard } from "@platform/lib/clipboard";
import { useCanRunInTerminal, openTerminal } from "@platform/lib/terminalDockStore";

const COPY_RESET_MS = 2000;

export function CommandLine({
  command,
  /** True while the row's own server-side action (install/sign-in) is
   * already busy running — Run stays disabled rather than letting a second
   * click start a second one on top of it. */
  disabled = false,
}: {
  command: string;
  disabled?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const canRun = useCanRunInTerminal();
  return (
    <div className="update-badge-command">
      <code>{command}</code>
      {canRun && (
        <button
          type="button"
          className="update-badge-run"
          disabled={disabled}
          onClick={() => openTerminal({ command })}
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
