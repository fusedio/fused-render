// The status-bar terminal's chip (PLAN-status-bar-terminal.md Task 5): a
// plain `StatusChip` toggled by `terminalDockStore.ts`'s local open state —
// deliberately NOT `useStatusChip`/`useExclusiveSection`, see that store's
// own header for why (hover-to-preview is wrong for a surface you type
// into, and the drawer reserves its own height so it has nothing to
// arbitrate space with).
//
// SPLIT INTO A PURE VIEW (`TerminalDockView`) AND A STATEFUL WRAPPER
// (`TerminalDock`, default export) — the same split `ModelsDock.tsx`'s own
// header describes for the identical reason: no store subscription, so
// `TerminalDock.test.tsx` can render the view directly with fixed props.
//
// NO SESSION-COUNT/FOREGROUND-COMMAND LABEL YET: the plan's own Task 5 text
// mentions "the running foreground command or shell name when open, count
// from 2 sessions" as the eventual chip label, but this round only ever
// keeps one live drawer session (TerminalDrawer.tsx owns a single session
// id) — there is no multi-session list to count or read a foreground command
// off yet. The label here is "Terminal" in both states; the tone flips
// on/off with `open` so the chip still visibly tracks the drawer. Revisit
// once multi-session support exists.
import StatusChip from "@platform/ui/StatusChip";
import { toggleTerminalDock, useTerminalDockOpen } from "@shell/terminalDockStore";

// The tooltip advertises VS Code's own Ctrl+` binding, not the user's
// Cmd/Ctrl+Shift+` chord (TerminalDrawer.tsx's own header) — that one is a
// reliable alias on every platform, while the Cmd version can collide with
// macOS's own window-cycling shortcut and never reach the page at all.
const SHORTCUT_HINT = "⌃`";

export function TerminalDockView({
  open,
  onToggle,
}: {
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="dl-host">
      <StatusChip
        label="Terminal"
        tone={open ? "on" : "idle"}
        open={open}
        title={open ? `Hide terminal (${SHORTCUT_HINT})` : `Show terminal (${SHORTCUT_HINT})`}
        ariaLabel={open ? "Terminal, open" : "Terminal"}
        onClick={onToggle}
      />
    </div>
  );
}

export default function TerminalDock() {
  const open = useTerminalDockOpen();
  return <TerminalDockView open={open} onToggle={toggleTerminalDock} />;
}
