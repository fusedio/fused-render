// The bar above the composer while messages are picked for a breakout. Without
// it the shift-click selection is invisible except for a faint tint on the
// turns themselves.
import { GitBranchPlus } from "lucide-react";

export interface BreakoutSelectionBarProps {
  /** Exchanges picked (user turns in the selection). */
  count: number;
  onOpen: () => void;
  onClear: () => void;
}

export function BreakoutSelectionBar({ count, onOpen, onClear }: BreakoutSelectionBarProps) {
  if (count <= 0) return null;
  return (
    <div className="breakout-sel-bar" role="status">
      <span className="breakout-sel-text">
        <GitBranchPlus size={14} aria-hidden="true" />
        <span>
          {count} {count === 1 ? "exchange" : "exchanges"} picked
        </span>
      </span>
      <span className="breakout-sel-actions">
        <button type="button" className="breakout-sel-open" onClick={onOpen}>
          Open breakout
        </button>
        <button type="button" className="breakout-sel-clear" onClick={onClear}>
          Clear
        </button>
      </span>
    </div>
  );
}
