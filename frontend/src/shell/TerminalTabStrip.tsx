// The terminal drawer's tab strip: one tab per terminal (label + close), the
// active one highlighted, and a "+" for a new one. Purely presentational —
// TerminalDrawer.tsx owns the list, the active id and every side effect — so
// TerminalDrawer.test.tsx can render it with fixed props and click through it.
import type { TerminalTab } from "@shell/terminalTabs";

export interface TerminalTabStripProps {
  tabs: TerminalTab[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
  onNew: () => void;
  /** Open a Claude chat that points at this terminal. Omitted = no button. */
  onAskClaude?: (id: string) => void;
  /** Stop what a Claude tab's chat is running. Omitted = no Stop button. */
  onStop?: (id: string) => void;
}

export default function TerminalTabStrip({ tabs, activeId, onSelect, onClose, onNew, onAskClaude, onStop }: TerminalTabStripProps) {
  return (
    <div className="term-tabs" role="tablist" aria-label="Terminals">
      {tabs.map((tab) => {
        const active = tab.id === activeId;
        if (tab.kind === "claude") {
          // The read-only tab of what Claude is running: no close (it follows
          // the chat, not the user), no Ask-Claude, a Stop while a command runs.
          return (
            <div
              key={tab.id}
              className={"term-tab is-claude" + (active ? " is-active" : "")}
              role="tab"
              aria-selected={active}
              title="What Claude is running (read-only)"
            >
              <button type="button" className="term-tab-label" onClick={() => onSelect(tab.id)}>
                {tab.label}
                {tab.running && <span className="term-tab-live" role="img" aria-label="running" />}
              </button>
              {tab.running && onStop && (
                <button
                  type="button"
                  className="term-tab-stop"
                  aria-label="Stop Claude's running command"
                  title="Stop the command Claude is running"
                  onClick={() => onStop(tab.id)}
                >
                  ■
                </button>
              )}
            </div>
          );
        }
        return (
          <div
            key={tab.id}
            className={"term-tab" + (active ? " is-active" : "")}
            role="tab"
            aria-selected={active}
            title={tab.cwd ? `${tab.label} — ${tab.cwd}` : tab.label}
          >
            <button type="button" className="term-tab-label" onClick={() => onSelect(tab.id)}>
              {tab.label}
            </button>
            {onAskClaude && (
              <button
                type="button"
                className="term-tab-ask"
                aria-label={`Ask Claude about ${tab.label}`}
                title="Ask Claude about this terminal"
                onClick={() => onAskClaude(tab.id)}
              >
                ✦
              </button>
            )}
            <button
              type="button"
              className="term-tab-close"
              aria-label={`Close ${tab.label}`}
              title="Close terminal (ends its shell)"
              onClick={() => onClose(tab.id)}
            >
              ×
            </button>
          </div>
        );
      })}
      <button type="button" className="term-tab-new" aria-label="New terminal" title="New terminal" onClick={onNew}>
        +
      </button>
    </div>
  );
}
