// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters hidden until the cog in the
// stage's title row asks for them (D430, D431, reshaped). Each control is a
// slider+number pair with a one-line hint, defaults baked in and a
// per-control reset once a value moves.
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { Check, Copy, RefreshCw, Settings } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@platform/shadcn/ui/card";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia } from "@platform/shadcn/ui/empty";
import { Field, FieldContent, FieldDescription, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Slider } from "@platform/shadcn/ui/slider";
import { Toggle } from "@platform/shadcn/ui/toggle";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { capabilityIcon } from "./capabilityIcons";

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
      <Toggle
        size="sm"
        className="min-w-0 flex-none px-1.5"
        pressed={configOpen}
        onPressedChange={onToggleConfig}
        aria-expanded={configOpen}
        aria-label={configOpen ? "Hide the settings" : "Show the settings"}
        title={configOpen ? "Hide the settings" : "Show the settings"}
      >
        <Settings aria-hidden="true" />
      </Toggle>
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
/** The panel's open state, as the one hook every stage uses: closed by
 *  default, and `touched` remembers whether the cog has ever been clicked.
 *  ConfigPanel animates its entry only when it has — a card already open at
 *  page load must not fade in a beat after everything else. */
export function useConfigOpen() {
  const [open, setOpen] = useState(false);
  const touched = useRef(false);
  const toggle = useCallback(() => {
    touched.current = true;
    setOpen((now) => !now);
  }, []);
  return { open, toggle, touched };
}

export function ConfigPanel({
  open,
  animated = true,
  children,
}: {
  open: boolean;
  /** False on a mount the user did not cause (the initial load): the fold-in
   *  animation and its wait are skipped, the card is just there. */
  animated?: boolean;
  children: ReactNode;
}) {
  // Mount is kept for the length of the exit so the card can fade OUT as well
  // as in: a panel that pops out of existence while the column glides back
  // under it is the half-animated version, and reads worse than no animation
  // at all. This unmount is also what STARTS the column's glide back: the
  // stylesheet holds the open geometry for as long as a closing card is
  // mounted (`:has(.pg-config-card.is-closing)`, ai-playground.css), because
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
      className={
        "pg-config-card" + (open ? "" : " is-closing") + (animated ? "" : " no-entry")
      }
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
        <CardContent className="flex flex-col gap-4">{children}</CardContent>
      </Card>
    </aside>
  );
}

/** Copy, as an icon in a result card's top-right corner. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      className="absolute top-2 right-2"
      title={copied ? "Copied" : label}
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
    </Button>
  );
}

/** One continuous parameter: label row (name, live value, reset), slider,
 *  hint. The number is editable — research note: sliders alone hide the range
 *  and playgrounds pair them with a numeric input.
 *
 *  The input holds a DRAFT string and commits on blur or Enter. Clamping on
 *  every keystroke made the box untypeable: "1" on the way to "1024" was
 *  clamped to the 256 floor before the next digit landed, and clearing the box
 *  read as 0. A caller that derives one value from another (the image stage's
 *  ratio lock) also wants one commit per edit, not one per digit. */
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
  const [draft, setDraft] = useState(String(value));
  // A chip, a slider drag or a reset changed the value from outside the box.
  useEffect(() => {
    setDraft(String(value));
  }, [value]);
  const commit = () => {
    const next = Number(draft);
    if (draft.trim() === "" || !Number.isFinite(next)) {
      setDraft(String(value));
      return;
    }
    // Onto the rail's grid, then inside its bounds — a typed 500 on a step of
    // 16 becomes 496, the same number the slider could have produced, so a
    // value derived from it (the image stage's other side) stays on-grid too.
    // The rounding keeps 0.1 steps from committing 0.30000000000000004.
    const decimals = (String(step).split(".")[1] ?? "").length;
    const stepped = Number((Math.round((next - min) / step) * step + min).toFixed(decimals));
    const clamped = Math.min(max, Math.max(min, stepped));
    setDraft(String(clamped));
    if (clamped !== value) onChange(clamped);
  };
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
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            }
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
      <FieldDescription className="text-xs leading-snug">{hint}</FieldDescription>
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
      {hint && <FieldDescription className="text-xs leading-snug">{hint}</FieldDescription>}
    </Field>
  );
}

/** A boolean setting: checkbox beside its name-and-hint. The label is tied to
 *  the checkbox by id, NOT by wrapping the Field in a FieldLabel — a
 *  FieldLabel with a Field child is shadcn's choice-card composition, and
 *  brings the card's border, padding and checked-highlight with it. */
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
  const id = useId();
  return (
    <Field orientation="horizontal" className="items-start gap-2">
      <Checkbox
        id={id}
        className="mt-0.5"
        checked={checked}
        onCheckedChange={(next) => onChange(!!next)}
      />
      <FieldContent className="gap-0.5">
        <FieldLabel htmlFor={id} className="text-xs font-semibold">
          {label}
        </FieldLabel>
        <FieldDescription className="text-xs leading-snug">{hint}</FieldDescription>
      </FieldContent>
    </Field>
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
    <ToggleGroup
      className="pg-chips w-full"
      value={active === null ? [] : [active]}
      onValueChange={(picked: unknown[]) => {
        const next = picked[0];
        if (typeof next === "string") onPick(next as T);
      }}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          variant="outline"
          size="sm"
          className="flex-none rounded-full tabular-nums"
          title={option.title}
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
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
          <Button
            key={sample.name}
            variant="outline"
            size="sm"
            className="flex-none rounded-full"
            // The pill shows a name; the prompt it stands for is only legible
            // on hover, so the title is load-bearing here, not decoration.
            title={sample.detail ?? sample.prompt}
            onClick={() => onPick(sample)}
          >
            <span data-icon="inline-start" aria-hidden="true">
              {sample.icon}
            </span>
            {sample.name}
          </Button>
        ))}
      </div>
      {samples.length > page && (
        <Button
          variant="ghost"
          size="icon-sm"
          className="flex-none rounded-full"
          title="Show other examples"
          aria-label="Show other examples"
          onClick={() => setOffset((at) => (at + page) % samples.length)}
        >
          <RefreshCw aria-hidden="true" />
        </Button>
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
      <Empty className="min-h-[200px] border border-dashed">
        <EmptyHeader>
          <EmptyMedia variant="icon" aria-hidden="true">
            {capabilityIcon(capability)}
          </EmptyMedia>
          <EmptyDescription>{note}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    </div>
  );
}
