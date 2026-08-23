// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters hidden until the cog in the
// stage's title row asks for them (D430, D431, reshaped). Each control is a
// slider+number pair with a one-line hint, defaults baked in and a
// per-control reset once a value moves.
import { useState, type ReactNode } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";

/** The stage's one-line title, with the config cog right-aligned on the same
 *  row. The cog lives up here rather than under the input because the title
 *  row is the stage's own header — the settings belong to the stage, not to
 *  the prompt — and a toggle at the end of the heading is where a settings
 *  affordance is looked for. The open state belongs to the STAGE: the panel it
 *  reveals is a sibling further down the column, not a child of this row. */
export function StageHeader({
  title,
  configOpen,
  onToggleConfig,
}: {
  title: string;
  configOpen: boolean;
  onToggleConfig: () => void;
}) {
  return (
    <div className="pg-work-head">
      <h2 className="pg-work-title">{title}</h2>
      <button
        type="button"
        className={"pg-cog" + (configOpen ? " active" : "")}
        aria-expanded={configOpen}
        aria-label={configOpen ? "Hide the settings" : "Show the settings"}
        title={configOpen ? "Hide the settings" : "Show the settings"}
        onClick={onToggleConfig}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.6 1.6 0 0 0 .33 1.77l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.6 1.6 0 0 0-1.77-.33 1.6 1.6 0 0 0-.97 1.47V21a2 2 0 1 1-4 0v-.11a1.6 1.6 0 0 0-1.05-1.47 1.6 1.6 0 0 0-1.77.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.6 1.6 0 0 0 .33-1.77 1.6 1.6 0 0 0-1.47-.97H3a2 2 0 1 1 0-4h.11a1.6 1.6 0 0 0 1.47-1.05 1.6 1.6 0 0 0-.33-1.77l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.6 1.6 0 0 0 1.77.33H9a1.6 1.6 0 0 0 .97-1.47V3a2 2 0 1 1 4 0v.11a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.77-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.6 1.6 0 0 0-.33 1.77V9a1.6 1.6 0 0 0 1.47.97H21a2 2 0 1 1 0 4h-.11a1.6 1.6 0 0 0-1.47.97Z" />
        </svg>
      </button>
    </div>
  );
}

/** Where the uncommon parameters live, revealed by the cog above. Closed by
 *  default on purpose: the surface it hides behind has to read as a simple
 *  call. Unmounted while closed, not hidden — every control inside is driven
 *  by stage state, so nothing is lost by not rendering it. */
export function ConfigPanel({ open, children }: { open: boolean; children: ReactNode }) {
  if (!open) return null;
  return <div className="pg-config-body">{children}</div>;
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

/** Example prompts, as one horizontal row of chips directly under the input.
 *  Research's one consistent finding on empty inputs (Open WebUI chips, AI
 *  Studio gallery, Replicate pre-fills): a blank box gives no value, a
 *  clickable example gives immediate value. They sit under the box rather than
 *  in a centered empty state because that is where the thing they fill is —
 *  one glance from the cursor, and the row scrolls sideways rather than
 *  growing the column. */
export function StarterPrompts({
  prompts,
  onPick,
}: {
  prompts: string[];
  onPick: (prompt: string) => void;
}) {
  return (
    <div className="pg-starter-row">
      {prompts.map((prompt) => (
        <button
          key={prompt}
          type="button"
          className="pg-starter-chip"
          title={prompt}
          onClick={() => onPick(prompt)}
        >
          {prompt}
        </button>
      ))}
    </div>
  );
}
