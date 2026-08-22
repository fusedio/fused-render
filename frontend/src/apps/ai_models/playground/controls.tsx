// The Playground's settings rail vocabulary, shared by the three stages.
//
// The layout research settled on the hosted-playground convention (OpenAI, AI
// Studio, LM Studio): parameters live in a RIGHT RAIL beside the work area,
// each as a slider+number pair with a one-line plain-language hint under the
// label, defaults baked in and a quiet per-control reset when a value has
// moved. On narrow windows the rail collapses behind a "Controls" toggle —
// the stage owns that state; these are only the controls themselves.
import type { ReactNode } from "react";

export function RailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pg-rail-section">
      <h5 className="pg-rail-title">{title}</h5>
      {children}
    </section>
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
