// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters behind the Config fold
// (D430, D431). Each control is a slider+number pair with a one-line hint,
// defaults baked in and a per-control reset once a value moves.
import { useState, type ReactNode } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";

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

/** Copy, as an icon in a result card's top-right corner. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="pg-copy-btn"
      title={copied ? "Copied" : label}
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M20 6L9 17l-5-5" />
        </svg>
      ) : (
        MenuIcons.copy
      )}
    </button>
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
