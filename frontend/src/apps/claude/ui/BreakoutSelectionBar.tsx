// The pick-mode bar above the composer: shown only while the top-bar Breakout
// button has the chat in pick mode.
import type { BreakoutExpert } from "@platform/lib/breakouts";
import { useExperts } from "@platform/lib/experts";

export interface BreakoutSelectionBarProps {
  /** Exchanges picked (user turns in the selection). */
  count: number;
  /** "With": the expert the breakout runs with; offered only when there are any. */
  expert?: BreakoutExpert | undefined;
  onExpert: (s: BreakoutExpert | undefined) => void;
  onStart: () => void;
  onCancel: () => void;
}

export function BreakoutSelectionBar({ count, expert, onExpert, onStart, onCancel }: BreakoutSelectionBarProps) {
  const experts = useExperts();
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
        {experts.length ? (
          <label>
            With{" "}
            <select
              className="breakout-sel-cancel"
              value={expert?.id ?? ""}
              onChange={(e) => {
                const s = experts.find((x) => x.id === e.target.value);
                onExpert(s && { id: s.id, name: s.name, home: s.home });
              }}
            >
              <option value="">No expert</option>
              {experts.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
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
