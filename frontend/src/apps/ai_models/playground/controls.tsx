// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters hidden until the cog in the
// stage's title row asks for them (D430, D431, reshaped). Each control is a
// slider+number pair with a one-line hint, defaults baked in and a
// per-control reset once a value moves.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";

/** A titled group inside VideoStage's own settings rail — predates the rail's
 *  removal from the other four stages (D429) and outlived it, since Video
 *  still draws a main-column + rail shape (`pg-work-video`, ai-playground.css)
 *  rather than the shared Config fold, and so never grew the cog (D464).
 *  Video is the ONLY consumer left; the other four read their settings out of
 *  `ConfigPanel` below. */
export function RailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pg-rail-section">
      <h5 className="pg-rail-title">{title}</h5>
      {children}
    </section>
  );
}

/** The empty state's starter prompts, as a wrapped row of text-only chips —
 *  VideoStage's, and only VideoStage's. The other four stages moved to
 *  `StarterCards` (icon + name pills, measured to fit) when the rail went
 *  away; Video kept its rail, so it kept these. */
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

/** A composer textarea that grows with its own text, so a Shift+Enter newline
 *  is visible. Returns the ref to hand the textarea and the `grow` to call on
 *  change.
 *
 *  The height it writes is an inline px value, which means it goes STALE the
 *  moment the box's width changes and the same text rewraps to more lines: the
 *  box keeps its old height and quietly turns into a scroller. A Clear button
 *  appearing beside the prompt used to be enough to do it. Hence the observer —
 *  and it watches the WIDTH only, because reacting to a height change would
 *  loop on the very thing this callback does. */
export function useAutoGrow(max = 180) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const grow = useCallback(() => {
    const box = ref.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, max) + "px";
  }, [max]);
  useEffect(() => {
    const box = ref.current;
    if (!box || typeof ResizeObserver === "undefined") return;
    let last = box.clientWidth;
    const observer = new ResizeObserver(() => {
      if (box.clientWidth === last) return;
      last = box.clientWidth;
      grow();
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, [grow]);
  return { ref, grow };
}

/** The stage's one-line title, with the config cog right-aligned on the same
 *  row. The cog lives up here rather than under the input because the title
 *  row is the stage's own header — the settings belong to the stage, not to
 *  the prompt — and a toggle at the end of the heading is where a settings
 *  affordance is looked for. The open state belongs to the STAGE: the card it
 *  reveals is a sibling beside the column, not a child of this row. */
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

/** Where the uncommon parameters live, revealed by the cog above — a narrow
 *  card BESIDE the column rather than a band across it, so the settings sit in
 *  the stage's right gutter and the input/result column keeps reading top to
 *  bottom. Wide enough windows give the card the whole gutter and the column
 *  does not move at all; narrower ones let it borrow some column width (see
 *  `.pg-work.has-config`, ai-playground.css) rather than cover anything.
 *
 *  Closed by default on purpose: the surface it hides behind has to read as a
 *  simple call. Unmounted while closed, not hidden — every control inside is
 *  driven by stage state, so nothing is lost by not rendering it. */
export function ConfigPanel({ open, children }: { open: boolean; children: ReactNode }) {
  if (!open) return null;
  return (
    <aside className="pg-config-card" aria-label="Settings">
      {/* Two boxes, not one: beside the column the <aside> is a full-height
          rail and this inner box is what sticks inside it. */}
      <div className="pg-config-inner">
        <p className="pg-config-head">Settings</p>
        <div className="pg-config-body">{children}</div>
      </div>
    </aside>
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

/** One authored example. Three fields, because a prompt worth running is too
 *  long to be its own label: `prompt` is the detailed thing that gets run,
 *  `name` is the two-or-three words the pill shows, `icon` is what makes the
 *  row scannable at a glance (starterIcons.tsx).
 *
 *  Stages extend it — the embed stage's samples carry the corpus their query
 *  is searched against — which is why `StarterCards` is generic over the
 *  sample type and hands the WHOLE sample back on a pick rather than a string. */
export interface Starter {
  name: string;
  icon: ReactNode;
  prompt: string;
  /** What hover says, when the prompt alone does not say it. The embed stage's
   *  prompt is a three-word query, and the interesting half of that sample is
   *  what the query is searched AGAINST. */
  detail?: string;
  /** A picture the sample brings WITH it, as a URL the app serves
   *  (`/static/samples/…`). The image stage's edit examples carry one: an edit
   *  prompt with no photo to edit demonstrates nothing, and hunting for a
   *  suitable file is the step that stops somebody trying it at all. The stage
   *  decides what to do with it — this row only carries it, the way `detail`
   *  does. */
  image?: string;
}

/** How many pills a page WANTS. Four fills the 680px column as one row, and
 *  every stage authors eight, so rotate is two pages at full width. */
const STARTER_PAGE = 4;

/** …and the fewest it will fall to. The row never ellipsises a name — when the
 *  width runs out it shows one pill fewer (see the measure below), and the
 *  settings card's narrower column is the case that asked for it. Two rather
 *  than three because a long-named pair can outgrow a very narrow column too,
 *  and three clipped names are worse than two whole ones. */
const STARTER_MIN = 2;

/** Example prompts, as one row of outlined pills under the input: an icon and
 *  a short name each, with a round rotate button at the end (D465).
 *
 *  Research's one consistent finding on empty inputs (Open WebUI chips, AI
 *  Studio gallery, Replicate pre-fills): a blank box gives no value, a
 *  clickable example gives immediate value. They sit under the box rather than
 *  in a centered empty state because that is where the thing they fill is.
 *
 *  Only a PAGE of them shows, with rotate for the rest: eight full prompts laid
 *  out at once is a wall in front of the input, and the reader only needs one.
 *  Rotation steps by whatever is on screen and is modular over the authored
 *  order — no shuffle, so clicking back around lands on the same pills.
 *
 *  How many is on screen is MEASURED, never truncated: the pills hug their
 *  names, so a narrower column shows one pill fewer rather than four cut-off
 *  labels. See the layout effect below. */
export function StarterCards<S extends Starter>({
  samples,
  onPick,
}: {
  samples: S[];
  onPick: (sample: S) => void;
}) {
  const [offset, setOffset] = useState(0);
  // How many fit, measured rather than guessed at a breakpoint: the pills hug
  // their names, so what fits depends on which four names are up — a threshold
  // in px would clip one page and leave a gap on another.
  const [page, setPage] = useState(STARTER_PAGE);
  const rowRef = useRef<HTMLDivElement>(null);
  // The loop is: ask for the full page, and while the row overflows, ask for
  // one fewer. It settles in at most two extra renders and cannot oscillate —
  // the row is `flex: 1`, so its own width does not change with the count.
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    if (page > STARTER_MIN && row.scrollWidth > row.clientWidth + 1) setPage(page - 1);
  }, [page, offset, samples]);
  // A resize re-opens the question upwards: dropping a pill is a one-way
  // ratchet within a layout, so a column that grows back (the settings card
  // closing) has to re-ask for the full page and let the measure trim again.
  useEffect(() => {
    const row = rowRef.current;
    if (!row || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setPage(STARTER_PAGE));
    observer.observe(row);
    return () => observer.disconnect();
  }, []);
  const shown = Array.from({ length: Math.min(page, samples.length) }, (_, at) => {
    return samples[(offset + at) % samples.length];
  });
  return (
    <div className="pg-starters">
      <div className="pg-starter-grid" ref={rowRef}>
        {shown.map((sample) => (
          <button
            key={sample.name}
            type="button"
            className="pg-starter-card"
            // The pill shows a name; the prompt it stands for is only legible
            // on hover, so the title is load-bearing here, not decoration.
            title={sample.detail ?? sample.prompt}
            onClick={() => onPick(sample)}
          >
            <span className="pg-starter-icon" aria-hidden="true">
              {sample.icon}
            </span>
            <span className="pg-starter-name">{sample.name}</span>
          </button>
        ))}
      </div>
      {samples.length > page && (
        <button
          type="button"
          className="pg-starter-rotate"
          title="Show other examples"
          aria-label="Show other examples"
          onClick={() => setOffset((at) => (at + page) % samples.length)}
        >
          {MenuIcons.refresh}
        </button>
      )}
    </div>
  );
}
