// The Playground's parameter vocabulary, shared by the stages.
//
// Every stage is one centered column reading as an API surface: input, Run,
// result — with all four capabilities' parameters hidden until the cog in the
// stage's title row asks for them (D430, D431, reshaped). The settings live in
// a right-side properties panel (Flow rule 8: a panel over a modal), reached
// through a portal so the panel sits beside the WHOLE stage column while its
// state stays with the stage that owns it. Each control is a property row —
// label left, value right — with the slider or picker beneath.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Check, Copy, RefreshCw, Settings2, X } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { Kbd } from "@platform/shadcn/ui/kbd";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@platform/shadcn/ui/select";
import { Slider } from "@platform/shadcn/ui/slider";
import { Switch } from "@platform/shadcn/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { PropertiesPanel, PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { SectionHeading, Tiny } from "@platform/ui/flow/Typography";
import { capabilityIcon } from "./capabilityIcons";

/** The stage's one-line title, with the config cog right-aligned on the same
 *  row. The cog lives up here rather than under the input because the title
 *  row is the stage's own header — the settings belong to the stage, not to
 *  the prompt — and a toggle at the end of the heading is where a settings
 *  affordance is looked for. The open state belongs to the STAGE: the panel it
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
    <div className="flex items-center justify-between gap-3">
      <h2 className="m-0 text-sm font-semibold">{title}</h2>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className={cn("text-muted-foreground", configOpen && "bg-muted text-foreground")}
        aria-expanded={configOpen}
        aria-label={configOpen ? "Hide the settings" : "Show the settings"}
        title={configOpen ? "Hide the settings" : "Show the settings"}
        onClick={onToggleConfig}
      >
        <Settings2 />
      </Button>
    </div>
  );
}

/** How long the settings panel's exit fade runs, in ms. The timer below
 *  unmounts the panel; the utility class fades it; a value that disagrees
 *  either cuts the fade off mid-way or leaves an invisible panel mounted. */
const CONFIG_EXIT_MS = 160;

/** The panel's open state, as the one hook every stage uses: closed by
 *  default, and `touched` remembers whether the cog has ever been clicked.
 *  ConfigPanel animates its entry only when it has — a panel already open at
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

/** Where the properties panel is drawn: PlaygroundTab renders an empty slot
 *  as the right-hand sibling of the scrolling stage column and provides it
 *  here; `ConfigPanel` portals into it. Null (no provider, or the slot not yet
 *  mounted) falls back to rendering in place. */
export const PanelSlotContext = createContext<HTMLElement | null>(null);

export function ConfigPanel({
  open,
  animated = true,
  children,
}: {
  open: boolean;
  /** False on a mount the user did not cause (the initial load): the fade-in
   *  is skipped, the panel is just there. */
  animated?: boolean;
  children: ReactNode;
}) {
  const slot = useContext(PanelSlotContext);
  // Mount is kept for the length of the exit so the panel can fade OUT as well
  // as in. Opacity only (the brief's rule): the column beside it snaps to its
  // new width, the panel itself fades.
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
  const panel = (
    <PropertiesPanel
      aria-label="Settings"
      data-state={open ? "open" : "closing"}
      // On the way out it is a picture of a panel, not a panel: nothing in it
      // can be reached or read while it fades.
      aria-hidden={open ? undefined : true}
      className={cn(
        "h-full min-h-0 motion-safe:transition-opacity motion-safe:duration-150 motion-safe:ease-out motion-reduce:transition-none",
        open ? "opacity-100" : "pointer-events-none opacity-0",
        open && animated && "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-150",
        !slot && "w-full border-t border-l-0 border-border",
      )}
    >
      <SectionHeading className="mb-3">Settings</SectionHeading>
      <PropertyList className="space-y-4">{children}</PropertyList>
    </PropertiesPanel>
  );
  return slot ? createPortal(panel, slot) : panel;
}

/** Copy, as an icon in a result card's top-right corner. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      className="absolute top-2 right-2 text-muted-foreground"
      title={copied ? "Copied" : label}
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? <Check /> : <Copy />}
    </Button>
  );
}

/** One continuous parameter: property row (name, editable value, reset), the
 *  slider, a one-line hint. The number is editable — research note: sliders
 *  alone hide the range and playgrounds pair them with a numeric input.
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
    <div className="space-y-1.5">
      <PropertyRow label={label} className="items-center py-0">
        <span className="inline-flex items-center gap-2">
          {moved && (
            <RailReset title={`Back to ${fallback}`} onClick={() => onChange(fallback)}>
              reset
            </RailReset>
          )}
          <Input
            className="h-6 w-16 shrink-0 px-1.5 text-right text-xs tabular-nums [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
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
        </span>
      </PropertyRow>
      {/* Base UI wants an array here — a bare number renders a thumb per
          bound (see slider.tsx's _values fallback), i.e. two of them. */}
      <Slider
        min={min}
        max={max}
        step={step}
        value={[value]}
        onValueChange={(next) => onChange(Array.isArray(next) ? next[0] : next)}
      />
      <Tiny className="block leading-snug">{hint}</Tiny>
    </div>
  );
}

/** The panel's "back to the default" affordance — a bare dotted-underline
 *  word, deliberately quieter than a Button: it sits inside a property row and
 *  must not compete with the value beside it. */
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
    <Button
      type="button"
      variant="link"
      size="xs"
      className="h-auto px-0 text-[11px] text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
      title={title}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}

/** One non-slider setting: property row (name, optional quiet action on the
 *  right), the control itself, a one-line hint. The shape every stage's
 *  bespoke rows (seed, language, system prompt…) share. */
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
    <div className="space-y-1.5">
      <PropertyRow label={label} className="items-center py-0">
        {action ?? null}
      </PropertyRow>
      {children}
      {hint && <Tiny className="block leading-snug">{hint}</Tiny>}
    </div>
  );
}

/** A boolean setting: a switch on the property row, its hint beneath. */
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
    <div className="space-y-1">
      <PropertyRow label={<label htmlFor={id}>{label}</label>} className="items-center py-0">
        <Switch id={id} size="sm" checked={checked} onCheckedChange={(next) => onChange(!!next)} />
      </PropertyRow>
      <Tiny className="block leading-snug">{hint}</Tiny>
    </div>
  );
}

/** A small closed picker, in the panel's width. */
export function RailSelect<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label?: string;
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as T)}>
      <SelectTrigger size="sm" className="w-full" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** A row of exclusive choices — aspect ratios, speed presets. `active` may
 *  match none of them (a hand-edited URL, a custom size), and then no chip
 *  lights: the chips are a VIEW over the underlying params, not the params. */
export function RailChips<T extends string>({
  options,
  active,
  onPick,
  label,
}: {
  options: { value: T; label: string; title?: string }[];
  active: T | null;
  onPick: (value: T) => void;
  label?: string;
}) {
  return (
    <ToggleGroup
      value={active === null ? [] : [active]}
      onValueChange={(v) => {
        const next = (v as string[])[0];
        if (next !== undefined) onPick(next as T);
      }}
      variant="outline"
      size="sm"
      spacing={1}
      // WRAPS, never scrolls sideways. In the panel's 320px these rows are
      // wider than the column (six aspect ratios, four step presets), and a
      // horizontal scroller with no visible scrollbar hides its last chip
      // behind a gesture nothing announces — the "Custom" aspect was
      // unreachable-looking at the panel's right edge. A second line costs
      // 24px and shows every choice at once.
      className="max-w-full flex-wrap"
      aria-label={label}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          className="rounded-full px-2.5 text-xs tabular-nums"
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
  /** What hover says, when the prompt alone does not say it. */
  detail?: string;
  /** A picture the sample brings WITH it, as a URL the app serves
   *  (`/static/samples/…`). The image stage's edit examples carry one. */
  image?: string;
}

/** How many pills a page WANTS. Four fills the column as one row, and every
 *  stage authors eight, so rotate is two pages at full width. */
const STARTER_PAGE = 4;

/** …and the fewest it will fall to. The row never ellipsises a name — when the
 *  width runs out it shows one pill fewer (see the measure below). */
const STARTER_MIN = 2;

/** Example prompts, as one row of outlined pills under the input: an icon and
 *  a short name each, with a round rotate button at the end (D465).
 *
 *  Only a PAGE of them shows, with rotate for the rest. Rotation steps by
 *  whatever is on screen and is modular over the authored order — no shuffle.
 *
 *  How many is on screen is MEASURED, never truncated: the pills hug their
 *  names, so a narrower column shows one pill fewer rather than four cut-off
 *  labels. The measure needs three things on the row: `overflow-hidden`,
 *  `min-w-0 flex-1` (so the box's width does not depend on what is in it) and
 *  `whitespace-nowrap` pills. */
export function StarterCards<S extends Starter>({
  samples,
  onPick,
}: {
  samples: S[];
  onPick: (sample: S) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState(STARTER_PAGE);
  // Bumped on every resize, and the measure below depends on it. Resetting
  // `page` is NOT enough to re-open the question: when the row is already
  // showing the full page, `setPage(STARTER_PAGE)` writes the value that is
  // already there, React bails out, no render happens and the measure never
  // re-runs. The settings panel opening did exactly that — the column lost
  // 320px and a fourth pill stayed half-drawn at the clip edge.
  const [probe, setProbe] = useState(0);
  const rowRef = useRef<HTMLDivElement>(null);
  // Ask for the full page, and while the row overflows, ask for one fewer. It
  // settles in at most two extra renders and cannot oscillate.
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    if (page > STARTER_MIN && row.scrollWidth > row.clientWidth + 1) setPage(page - 1);
  }, [page, offset, samples, probe]);
  // A resize re-opens the question upwards: dropping a pill is a one-way
  // ratchet within a layout, so a column that grows back has to re-ask for the
  // full page and let the measure trim again.
  useEffect(() => {
    const row = rowRef.current;
    if (!row || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      setPage(STARTER_PAGE);
      setProbe((at) => at + 1);
    });
    observer.observe(row);
    return () => observer.disconnect();
  }, []);
  const shown = Array.from({ length: Math.min(page, samples.length) }, (_, at) => {
    return samples[(offset + at) % samples.length];
  });
  return (
    <div className="flex items-start p-0.5">
      {/* `flex-1`, not `shrink`: the measure below compares this box's
          scrollWidth to its clientWidth, so the box's WIDTH must not depend on
          how many pills are in it. With a content-sized basis, dropping a pill
          narrowed the row, which re-fired the ResizeObserver, which asked for
          the full page again — the two fought, and a pill sat half-drawn at the
          clip edge whenever the column was too narrow for four (opening the
          settings panel did it). Filling the leftover space pins the width to
          the container, so the trim converges.
          Rotate lives INSIDE this box, as its last child: pinned outside a
          full-width row it drifted to the far right edge, a lone circle three
          hundred pixels from the pills it rotates. In here it follows the last
          pill, and the measure counts it — so it is never the thing that gets
          clipped. */}
      <div
        className="flex min-w-0 flex-1 flex-nowrap items-center gap-2 overflow-hidden"
        ref={rowRef}
      >
        {shown.map((sample) => (
          <Button
            key={sample.name}
            type="button"
            variant="outline"
            size="sm"
            className="flex-none rounded-full text-xs whitespace-nowrap text-muted-foreground hover:text-foreground [&_svg]:opacity-75 hover:[&_svg]:opacity-100"
            // The pill shows a name; the prompt it stands for is only legible
            // on hover, so the title is load-bearing here, not decoration.
            title={sample.detail ?? sample.prompt}
            onClick={() => onPick(sample)}
          >
            <span aria-hidden="true" className="flex">
              {sample.icon}
            </span>
            {sample.name}
          </Button>
        ))}
        {samples.length > page && (
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            className="flex-none rounded-full text-muted-foreground hover:text-foreground"
            title="Show other examples"
            aria-label="Show other examples"
            onClick={() => setOffset((at) => (at + page) % samples.length)}
          >
            <RefreshCw />
          </Button>
        )}
      </div>
    </div>
  );
}

/** Label above, box below — the result in both its states. `pg-answer-block`
 *  is a bare MARKER class with no stylesheet behind it: the AI tour
 *  (platform/lib/tours/ai.ts) spotlights this element by that name. */
export function AnswerBlock({
  label,
  status,
  provenance,
  children,
}: {
  label: string;
  /** A status the label carries beside it — `running` while streaming. */
  status?: string | null;
  /** Which model produced what is below — the embed stage's provenance. */
  provenance?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="pg-answer-block flex flex-col gap-2">
      <p className="m-0 flex min-w-0 items-baseline gap-2 text-xs font-semibold text-muted-foreground">
        {status && <StatusDot status={status} pulse className="self-center" />}
        {label}
        {provenance && (
          <span
            data-slot="answer-provenance"
            className="min-w-0 truncate font-normal tabular-nums opacity-75"
            title={`These scores were computed by ${provenance}. Vectors from two models are not comparable, even when they are the same size.`}
          >
            {provenance}
          </span>
        )}
      </p>
      {children}
    </div>
  );
}

/** The result canvas, idle. Every stage draws one where its answer will land,
 *  so a stage that has not run yet reads as *waiting* rather than as half a
 *  page. A PLACEHOLDER, not a skeleton: no shimmer, no fake rows — a dashed
 *  frame with the capability's own glyph and one line naming what arrives.
 *  One box, one height, on all five stages. */
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
    <AnswerBlock label={label}>
      <div className="flex min-h-[200px] flex-col items-center justify-center gap-2.5 rounded-lg border border-dashed border-border bg-muted/30 p-6 text-center">
        <span
          aria-hidden="true"
          className="grid place-items-center text-muted-foreground opacity-50 [&_svg]:size-6"
        >
          {capabilityIcon(capability)}
        </span>
        <Tiny className="block max-w-[34ch] leading-normal">{note}</Tiny>
      </div>
    </AnswerBlock>
  );
}

/** The filled result box — a bordered muted surface with room top-right for
 *  the copy button. */
export function AnswerBox({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "relative rounded-lg border border-border bg-muted/30 py-3.5 pr-11 pl-4 text-sm leading-relaxed [overflow-wrap:anywhere]",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** The focal composer: one square Card holding the input and the Run button.
 *  `pg-composer` is a bare MARKER class (see AnswerBlock) the AI tour targets;
 *  `stacked` puts the prompt across the whole box with a floor beneath it. */
export function ComposerCard({
  stacked,
  className,
  children,
}: {
  stacked?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "pg-composer relative flex gap-2 rounded-lg border border-border bg-card p-2 focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        stacked ? "flex-col items-stretch" : "items-end",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** The prompt box inside a ComposerCard — a PLAIN <textarea>/<input>, not the
 *  shadcn Textarea: that one is a function component with no forwardRef, and
 *  `useAutoGrow` needs the element. Chrome off (the card is the border). The
 *  two heights are what `useAutoGrow` depends on — a three-line floor and the
 *  ten-line backstop `COMPOSER_MAX_LINES` names (platform/lib/autoGrow.ts). */
export const composerTextareaClass =
  "field-sizing-fixed min-h-[calc(4.5em+12px)] max-h-[calc(15em+12px)] min-w-0 flex-1 resize-none border-0 bg-transparent px-1 py-1.5 font-[inherit] text-sm leading-normal text-foreground outline-none placeholder:text-muted-foreground";

/** The one-line input (the embed stage's search): top-aligned, so it starts
 *  where every other stage's prompt starts. */
export const composerInputClass =
  "min-w-0 flex-1 self-start border-0 bg-transparent px-1 py-1.5 font-[inherit] text-sm leading-normal text-foreground outline-none placeholder:text-muted-foreground";

/** The button column beside a row composer: Clear (when offered) above Run,
 *  in the bottom-right corner. Its 72px floor keeps a one-line composer from
 *  growing when Clear appears. */
export function ComposerSide({ floor = true, children }: { floor?: boolean; children: ReactNode }) {
  return (
    <div className={cn("flex flex-none flex-col items-end justify-end gap-2", floor && "min-h-[72px]")}>
      {children}
    </div>
  );
}

/** The one primary action of a stage, with its Enter hint. `pg-send` is a bare
 *  MARKER class for the AI tour. */
export function RunButton({ children, ...props }: Omit<ComponentProps<typeof Button>, "variant" | "size">) {
  return (
    <Button type="button" className="pg-send flex-none" {...props}>
      {children}
      <Kbd className="ml-1 bg-primary-foreground/15 text-primary-foreground">⏎</Kbd>
    </Button>
  );
}

/** Stop, in the Run slot, while a run is live. */
export function StopButton(props: Omit<ComponentProps<typeof Button>, "variant" | "size">) {
  return <Button type="button" variant="secondary" className="pg-send flex-none" {...props} />;
}

/** The quiet Clear beside a composer. `corner` floats it in a stacked
 *  composer's top-right, out of flow, so it adds no height. */
export function ClearButton({
  corner,
  className,
  ...props
}: Omit<ComponentProps<typeof Button>, "variant" | "size"> & { corner?: boolean }) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className={cn("text-muted-foreground", corner && "absolute top-2 right-2 z-10", className)}
      {...props}
    >
      Clear
    </Button>
  );
}

/** A one-line status or error under the composer, with its dot. */
export function StatusLine({ status, children }: { status: "loading" | "error"; children: ReactNode }) {
  return (
    <p
      className={cn(
        "m-0 flex items-center gap-2 text-xs",
        status === "error" ? "text-destructive" : "text-muted-foreground",
      )}
    >
      <StatusDot status={status} pulse={status === "loading"} />
      <span>{children}</span>
    </p>
  );
}

/** A neutral progress bar — the fraction of a job done, not a threshold
 *  meter, so it wears the primary fill. */
export function ProgressBar({ pct }: { pct: number }) {
  return (
    <span className="block h-1 w-full max-w-80 overflow-hidden rounded-full bg-muted">
      <span
        className="block h-full bg-primary motion-safe:transition-[width] motion-safe:duration-300"
        style={{ width: `${pct}%` }}
      />
    </span>
  );
}

/** A picture (or the webcam) at full size, over everything — the whole modal,
 *  no title bar beyond the accessible one. Escape and the backdrop close it,
 *  the two things anybody tries. */
export function Lightbox({
  open,
  onClose,
  label,
  closeLabel = "Close",
  children,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  closeLabel?: string;
  children: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100vh-4rem)] w-fit max-w-[calc(100vw-4rem)] flex-col items-center gap-3 p-3 sm:max-w-[calc(100vw-4rem)]"
      >
        <DialogTitle className="sr-only">{label}</DialogTitle>
        {children}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="absolute top-2 right-2"
          title={closeLabel}
          aria-label={closeLabel}
          onClick={onClose}
        >
          <X />
        </Button>
      </DialogContent>
    </Dialog>
  );
}

/** The attached picture, as a chip on the composer's floor: the thumbnail
 *  (opens the lightbox) and the ✕ that removes it. */
export function AttachChip({
  src,
  onOpen,
  onRemove,
}: {
  src: string;
  onOpen: () => void;
  onRemove: () => void;
}) {
  return (
    <span className="mr-auto inline-flex items-center gap-1 rounded-md border border-border bg-background p-0.5">
      <button
        type="button"
        className="block cursor-zoom-in rounded-sm border-0 bg-transparent p-0 leading-none hover:opacity-80"
        title="See this picture"
        aria-label="See this picture"
        onClick={onOpen}
      >
        <img src={src} alt="" className="size-7 rounded-sm bg-muted object-cover" />
      </button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="size-5.5 text-muted-foreground"
        title="Remove this image"
        aria-label="Remove this image"
        onClick={onRemove}
      >
        <X />
      </Button>
    </span>
  );
}

/** One way to attach a picture — a small outlined pill with an icon. */
export function AttachButton({
  active,
  className,
  ...props
}: Omit<ComponentProps<typeof Button>, "variant" | "size"> & { active?: boolean }) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn(
        "h-7 rounded-full text-xs text-muted-foreground hover:text-foreground",
        active && "border-ring text-foreground",
        className,
      )}
      {...props}
    />
  );
}
