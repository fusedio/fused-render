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
}

export default function TerminalTabStrip({ tabs, activeId, onSelect, onClose, onNew }: TerminalTabStripProps) {
  return (
    <div className="term-tabs" role="tablist" aria-label="Terminals">
      {tabs.map((tab) => {
        const active = tab.id === activeId;
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
