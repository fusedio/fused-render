// The pick-mode bar above the composer: shown only while the top-bar Breakout
// button has the chat in pick mode.
export interface BreakoutSelectionBarProps {
  /** Exchanges picked (user turns in the selection). */
  count: number;
  onStart: () => void;
  onCancel: () => void;
}

export function BreakoutSelectionBar({ count, onStart, onCancel }: BreakoutSelectionBarProps) {
  return (
    <div className="breakout-sel-bar" role="status">
      <span className="breakout-sel-text">
        {count > 0 ? (
          <span>
            <b>{count}</b> {count === 1 ? "exchange" : "exchanges"} picked
          </span>
        ) : (
          <span>Click messages to pick them for a breakout</span>
        )}
      </span>
      <span className="breakout-sel-actions">
        <button type="button" className="breakout-sel-cancel" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="breakout-sel-start" disabled={count <= 0} onClick={onStart}>
          Start breakout
        </button>
      </span>
    </div>
  );
}
