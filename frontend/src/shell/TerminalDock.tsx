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
// SESSION COUNT: the label stays "Terminal" in both states and the tone flips
// on/off with `open`; once the drawer holds more than one terminal the chip
// also shows how many (`StatusChip`'s own grey count pill, which hides a
// count of 0 — and a lone terminal is passed as 0, since one terminal needs
// no number). `TerminalDrawer.tsx` owns the tab list and publishes the
// count through `terminalDockStore.ts`.
import StatusChip from "@platform/ui/StatusChip";
import { toggleTerminalDock, useTerminalCount, useTerminalDockOpen } from "@platform/lib/terminalDockStore";

// The tooltip advertises VS Code's own Ctrl+` binding, not the user's
// Cmd/Ctrl+Shift+` chord (TerminalDrawer.tsx's own header) — that one is a
// reliable alias on every platform, while the Cmd version can collide with
// macOS's own window-cycling shortcut and never reach the page at all.
const SHORTCUT_HINT = "⌃`";

export function TerminalDockView({
  open,
  onToggle,
  count = 0,
}: {
  open: boolean;
  onToggle: () => void;
  /** Terminals held by the drawer; shown only from 2. */
  count?: number;
}) {
  const shown = count > 1 ? count : 0;
  return (
    <div className="dl-host">
      <StatusChip
        label="Terminal"
        count={shown}
        tone={open ? "on" : "idle"}
        open={open}
        title={open ? `Hide terminal (${SHORTCUT_HINT})` : `Show terminal (${SHORTCUT_HINT})`}
        ariaLabel={(open ? "Terminal, open" : "Terminal") + (shown > 0 ? `, ${shown} terminals` : "")}
        onClick={onToggle}
      />
    </div>
  );
}

export default function TerminalDock() {
  const open = useTerminalDockOpen();
  const count = useTerminalCount();
  return <TerminalDockView open={open} onToggle={toggleTerminalDock} count={count} />;
}
