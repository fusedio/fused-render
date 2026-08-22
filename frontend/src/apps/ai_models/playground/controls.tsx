// The Playground's parameter vocabulary, shared by the stages.
//
// The unified stage shape (all four capabilities) is one centered column that
// reads as an API surface: prompt, Run, result. The text and image stages put
// EVERY parameter behind the Config fold (D430, D431) so the surface above it
// is prompt and Run alone; transcription keeps its Task select inline
// (`.pg-params`). Each control is a slider+number pair with a one-line
// plain-language hint, defaults baked in and a quiet per-control reset when a
// value has moved.
import type { ReactNode } from "react";

/** The fold everything uncommon goes behind. Closed by default on purpose:
 *  the panel's job is to make the surface above it read as a simple call. */
export function AdvancedPanel({ children }: { children: ReactNode }) {
  return (
    <details className="pg-config">
      <summary>Config</summary>
      <div className="pg-config-body">{children}</div>
    </details>
  );
}

/** One continuous parameter: label row (name, live value, reset), slider,
 *  hint. The number is editable — research note: sliders alone hide the range
 *  and playgrounds pair them with a numeric input. */
export function RailSlider({
  label,
  hint,
  min,
  max,
  step,
  value,
  fallback,
  onChange,
}: {
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  value: number;
  fallback: number;
  onChange: (value: number) => void;
}) {
  const moved = value !== fallback;
  return (
    <label className="pg-ctl">
      <span className="pg-ctl-head">
        <span className="pg-ctl-label">{label}</span>
        {moved && (
          <button
            type="button"
            className="pg-ctl-reset"
            title={`Back to ${fallback}`}
            onClick={(e) => {
              e.preventDefault();
              onChange(fallback);
            }}
          >
            reset
          </button>
        )}
        <input
          className="pg-ctl-num"
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => {
            const next = Number(e.target.value);
            if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, next)));
          }}
        />
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <span className="pg-ctl-hint">{hint}</span>
    </label>
  );
}

/** A row of exclusive choices — aspect ratios, speed presets. `active` may
 *  match none of them (a hand-edited URL, a custom size), and then no chip
 *  lights: the chips are a VIEW over the underlying params, not the params. */
export function RailChips<T extends string>({
  options,
  active,
  onPick,
}: {
  options: { value: T; label: string; title?: string }[];
  active: T | null;
  onPick: (value: T) => void;
}) {
  return (
    <div className="pg-chips" role="group">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={"pg-chip" + (option.value === active ? " active" : "")}
          aria-pressed={option.value === active}
          title={option.title}
          onClick={() => onPick(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** The empty state's starter prompts: research's one consistent finding on
 *  empty states (Open WebUI chips, AI Studio gallery, Replicate pre-fills) —
 *  a blank box gives no value; a clickable example gives immediate value. */
export function StarterPrompts({
  title,
  prompts,
  onPick,
}: {
  title: string;
  prompts: string[];
  onPick: (prompt: string) => void;
}) {
  return (
    <div className="pg-starter">
      <p className="pg-starter-title">{title}</p>
      <div className="pg-starter-chips">
        {prompts.map((prompt) => (
          <button key={prompt} type="button" className="pg-starter-chip" onClick={() => onPick(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
