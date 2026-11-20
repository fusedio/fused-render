// The pick-mode bar above the composer: shown only while the top-bar Breakout
// button has the chat in pick mode.
import type { BreakoutSpecialist } from "@platform/lib/breakouts";
import { useSpecialists } from "@platform/lib/specialists";

export interface BreakoutSelectionBarProps {
  /** Exchanges picked (user turns in the selection). */
  count: number;
  /** "With": the specialist the breakout runs with; offered only when there are any. */
  specialist?: BreakoutSpecialist | undefined;
  onSpecialist: (s: BreakoutSpecialist | undefined) => void;
  onStart: () => void;
  onCancel: () => void;
}

export function BreakoutSelectionBar({ count, specialist, onSpecialist, onStart, onCancel }: BreakoutSelectionBarProps) {
  const specialists = useSpecialists();
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
        {specialists.length ? (
          <label>
            With{" "}
            <select
              className="breakout-sel-cancel"
              value={specialist?.id ?? ""}
              onChange={(e) => {
                const s = specialists.find((x) => x.id === e.target.value);
                onSpecialist(s && { id: s.id, name: s.name, home: s.home });
              }}
            >
              <option value="">No specialist</option>
              {specialists.map((s) => (
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
