// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters hidden until the cog in the
// stage's title row asks for them (D430, D431, reshaped). Each control is a
// slider+number pair with a one-line hint, defaults baked in and a
// per-control reset once a value moves.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Card, CardContent, CardHeader, CardTitle } from "@platform/shadcn/ui/card";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Field, FieldContent, FieldDescription, FieldLabel, FieldTitle } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Slider } from "@platform/shadcn/ui/slider";
import { capabilityIcon } from "./capabilityIcons";

/** A composer textarea that grows with its own text, so a Shift+Enter newline
 *  is visible. Returns the ref to hand the textarea and the `grow` to call on
 *  change.
 *
 *  The height it writes is an inline px value, which means it goes STALE the
 *  moment the box's width changes and the same text rewraps to more lines: the
 *  box keeps its old height and quietly turns into a scroller. A Clear button
 *  appearing beside the prompt used to be enough to do it. Hence the observer —
 *  and it watches the WIDTH only, because reacting to a height change would
 *  loop on the very thing this callback does.
 *
 *  CSS `field-sizing: content` would delete this hook outright. Deliberately
 *  not used: it is Chromium-only, and this sheet is not a Chromium-only
 *  sheet. Revisit when Safari and Firefox ship it. */
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

/** How long the settings card's exit animation runs, in ms. Mirrors
 *  `--pg-fade` in ai-playground.css — the timer below unmounts the card, the
 *  stylesheet fades it, and a value that disagrees either cuts the fade off
 *  mid-way or leaves an invisible card mounted after it. */
const CONFIG_EXIT_MS = 160;

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
  // Mount is kept for the length of the exit so the card can fade OUT as well
  // as in: a panel that pops out of existence while the column glides back
  // under it is the half-animated version, and reads worse than no animation
  // at all. This unmount is also what STARTS the column's glide back: the
  // stylesheet holds the open geometry for as long as a closing card is
  // mounted (`:has(> .pg-config-card.is-closing)`, ai-playground.css), because
  // the card's open place and the column's closed place overlap — a card
  // fading over a column already in motion can only be clipped by the stage or
  // laid on top of the composer.
  const [shown, setShown] = useState(open);
  useEffect(() => {
    if (open) {
      setShown(true);
      return;
    }
    if (!shown) return;
    const timer = window.setTimeout(() => setShown(false), CONFIG_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [open, shown]);
  if (!shown) return null;
  return (
    <aside
      className={"pg-config-card" + (open ? "" : " is-closing")}
      aria-label="Settings"
      // On the way out it is a picture of a panel, not a panel: nothing in it
      // can be reached or read while it fades.
      aria-hidden={open ? undefined : true}
    >
      {/* Two boxes, not one: beside the column the <aside> is a full-height
          rail and this inner box is what sticks inside it. The Card carries
          `pg-config-inner` so the sticky, fold-in/out and reduced-motion rules
          in ai-playground.css keep landing on it. */}
      <Card className="pg-config-inner flex-none">
        <CardHeader>
          <CardTitle className="text-[10.5px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">
            Settings
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">{children}</CardContent>
      </Card>
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
    <Field className="gap-1.5">
      <div className="flex w-full items-center gap-2">
        <FieldLabel className="text-xs font-semibold">{label}</FieldLabel>
        {moved && (
          <RailReset title={`Back to ${fallback}`} onClick={() => onChange(fallback)}>
            reset
          </RailReset>
        )}
        <Input
          className="ml-auto h-6 w-16 shrink-0 rounded-md px-1.5 text-right text-xs tabular-nums [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
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
      </div>
      {/* Base UI wants an array here — a bare number renders a thumb per
          bound (see slider.tsx's _values fallback), i.e. two of them. */}
      <Slider
        min={min}
        max={max}
        step={step}
        value={[value]}
        onValueChange={(next) => onChange(Array.isArray(next) ? next[0] : next)}
      />
      <FieldDescription className="text-xs leading-normal">{hint}</FieldDescription>
    </Field>
  );
}

/** The panel's "back to the default" affordance — a bare dotted-underline
 *  word, deliberately quieter than a Button: it sits inside a label row and
 *  must not compete with the value beside it. The appearance/border/background
 *  resets are load-bearing — preflight is off, so the UA's button chrome
 *  shows without them. */
export function RailReset({
  title,
  onClick,
  children,
}: {
  title?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="cursor-pointer appearance-none border-0 bg-transparent p-0 text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
      title={title}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

/** One non-slider setting: label row (name, optional quiet action on the
 *  right), the control itself, a one-line hint. The shape every stage's
 *  bespoke rows (seed, language, system prompt…) share, so the stages compose
 *  this instead of hand-rolling Field internals. */
export function RailField({
  label,
  action,
  hint,
  children,
}: {
  label: string;
  action?: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Field className="gap-1.5">
      <div className="flex w-full items-baseline gap-2">
        <FieldLabel className="text-xs font-semibold">{label}</FieldLabel>
        {action && <span className="ml-auto">{action}</span>}
      </div>
      {children}
      {hint && <FieldDescription className="text-xs leading-normal">{hint}</FieldDescription>}
    </Field>
  );
}

/** A boolean setting: checkbox beside its name-and-hint, all one click
 *  target (the FieldLabel wrapper is what makes the text toggle it). */
export function RailCheck({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <FieldLabel className="font-normal">
      <Field orientation="horizontal" className="items-start gap-2">
        <Checkbox
          className="mt-0.5"
          checked={checked}
          onCheckedChange={(next) => onChange(!!next)}
        />
        <FieldContent className="gap-0.5">
          <FieldTitle className="text-xs font-semibold">{label}</FieldTitle>
          <FieldDescription className="text-xs leading-normal">{hint}</FieldDescription>
        </FieldContent>
      </Field>
    </FieldLabel>
  );
}

/** A native <select> in the Input's clothes. Native on purpose — the two- and
 *  three-option pickers in the panel don't earn a popover — and the UA keeps
 *  its own dropdown arrow, so no `appearance-none`. */
export function RailSelect({
  className,
  ...props
}: ComponentProps<"select">) {
  return (
    <select
      data-slot="rail-select"
      className={
        "h-8 w-full rounded-lg border border-input bg-transparent px-2 text-sm transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30 " +
        (className ?? "")
      }
      {...props}
    />
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

/** The result canvas, idle. Every stage draws one where its answer will land,
 *  so a stage that has not run yet reads as *waiting* rather than as half a
 *  page — the empty band between the prompt and the app strip below was the
 *  loudest unfinished signal on the tab.
 *
 *  It is a PLACEHOLDER, not a skeleton: no shimmer, no fake rows. A dashed
 *  frame with the capability's own sidebar glyph and one line naming what
 *  arrives here — the same grammar an empty folder or an empty inbox gets.
 *
 *  One box, one height, on all five stages, rather than the aspect-locked
 *  frame the image and video renders actually fill. An aspect-locked idle
 *  frame would avoid the first render's layout shift, but a 9:16 placeholder
 *  is ~1.8 column widths of empty dashed box — trading the void this fixes for
 *  a taller one. The shift on first Generate is cheap and reads as the frame
 *  BECOMING the picture.
 *
 *  `label` repeats the filled block's own heading ("Result", "Response",
 *  "Transcript", …) and the slot sits in the SAME JSX position, so idle and
 *  filled are one box in two states rather than two siblings taking turns. */
export function ResultSlot({
  label,
  capability,
  note,
}: {
  label: string;
  capability: string;
  note: string;
}) {
  return (
    <div className="pg-answer-block">
      <p className="pg-answer-label">{label}</p>
      <div className="pg-slot">
        <span className="pg-slot-icon" aria-hidden="true">
          {capabilityIcon(capability)}
        </span>
        <p className="pg-slot-note">{note}</p>
      </div>
    </div>
  );
}
