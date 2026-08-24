// The Playground's parameter vocabulary, shared by the stages — shadcn/ui
// edition. Every stage is one centered column reading as an API surface:
// input, Run, result — with all parameters behind the cog in the stage's
// title row, revealed as a settings card in the column's right gutter.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { CheckIcon, CopyIcon, RefreshCwIcon, SettingsIcon } from "lucide-react";
import { Button } from "@apps/ai_models/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia } from "@apps/ai_models/ui/empty";
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldTitle,
} from "@apps/ai_models/ui/field";
import { Input } from "@apps/ai_models/ui/input";
import { Slider } from "@apps/ai_models/ui/slider";
import { ToggleGroup, ToggleGroupItem } from "@apps/ai_models/ui/toggle-group";
import { cn } from "@apps/ai_models/ui/utils";
import { capabilityIcon } from "./capabilityIcons";

/** A composer textarea that grows with its own text, so a Shift+Enter newline
 *  is visible. Returns the ref to hand the textarea and the `grow` to call on
 *  change. The height it writes is an inline px value, which goes stale when
 *  the box's width changes and the same text rewraps — hence the observer,
 *  which watches the WIDTH only (reacting to height would loop on the very
 *  thing this callback does). */
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

/** The stage's frame: title row with the config cog, a centered main column,
 *  and — while the cog is open — the settings card in the right gutter.
 *  Stages hand their controls in as `config`; the main column is `children`. */
export function StageShell({
  title,
  configOpen,
  onToggleConfig,
  config,
  children,
}: {
  title: string;
  configOpen: boolean;
  onToggleConfig: () => void;
  config: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-0 flex-1">
      {/* The main column: the API surface — input, Run, the result of that
          run — reading top to bottom on a comfortable measure. */}
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
            {/* On wide windows the settings rail is simply there; the cog
                exists for the widths where it is not. */}
            <Button
              type="button"
              variant={configOpen ? "secondary" : "ghost"}
              size="icon-sm"
              className="xl:hidden"
              aria-expanded={configOpen}
              aria-label={configOpen ? "Hide the settings" : "Show the settings"}
              title={configOpen ? "Hide the settings" : "Show the settings"}
              onClick={onToggleConfig}
            >
              <SettingsIcon />
            </Button>
          </div>
          {children}
        </div>
      </div>
      {/* The settings rail: a bordered pane, not a floating card — always
          visible on wide windows (a playground's knobs are half the point),
          behind the cog below xl. */}
      <aside
        className={cn(
          "w-72 shrink-0 overflow-y-auto border-l px-5 py-5",
          configOpen ? "block" : "hidden xl:block",
        )}
        aria-label="Settings"
      >
        <p className="pb-4 text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Settings
        </p>
        <FieldGroup className="gap-6">{config}</FieldGroup>
      </aside>
    </div>
  );
}

/** Copy, as an icon button in a result card's top-right corner. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      className="absolute top-2 right-2 text-muted-foreground"
      title={copied ? "Copied" : label}
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

/** One continuous parameter: label row (name, reset, editable number), slider,
 *  hint. The number is editable — sliders alone hide the range. */
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
    <Field>
      <FieldContent>
        <div className="flex items-center gap-2">
          <FieldTitle className="flex-1">{label}</FieldTitle>
          {moved && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              title={`Back to ${fallback}`}
              onClick={() => onChange(fallback)}
            >
              reset
            </Button>
          )}
          <Input
            type="number"
            className="h-7 w-20 px-2 text-right text-xs"
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
        <Slider
          min={min}
          max={max}
          step={step}
          value={[value]}
          onValueChange={(v) => onChange(v[0])}
        />
        <FieldDescription>{hint}</FieldDescription>
      </FieldContent>
    </Field>
  );
}

/** A row of exclusive choices — aspect ratios, speed presets. `active` may
 *  match none of them (a hand-edited URL, a custom size), and then no item
 *  lights: the group is a VIEW over the underlying params, not the params. */
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
      type="single"
      variant="outline"
      size="sm"
      spacing={1.5}
      className="w-full flex-wrap [&>*]:px-2 [&>*]:text-xs"
      value={active ?? ""}
      onValueChange={(value) => {
        if (value) onPick(value as T);
      }}
    >
      {options.map((option) => (
        <ToggleGroupItem key={option.value} value={option.value} title={option.title}>
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

/** One authored example. Three fields, because a prompt worth running is too
 *  long to be its own label: `prompt` is the detailed thing that gets run,
 *  `name` is the two-or-three words the pill shows, `icon` makes the row
 *  scannable. Stages extend it — hence the generic `StarterCards`, which
 *  hands the WHOLE sample back on a pick rather than a string. */
export interface Starter {
  name: string;
  icon: ReactNode;
  prompt: string;
  /** What hover says, when the prompt alone does not say it. */
  detail?: string;
  /** A picture the sample brings WITH it, as a URL the app serves. */
  image?: string;
}

/** How many pills a page WANTS — four fills the column as one row. */
const STARTER_PAGE = 4;
/** …and the fewest it will fall to: the row never ellipsises a name. */
const STARTER_MIN = 2;

/** Example prompts, as one row of outlined pills under the input, with a
 *  round rotate button at the end. Only a PAGE shows; rotation is modular
 *  over the authored order. How many is on screen is MEASURED, never
 *  truncated: a narrower column shows one pill fewer. */
export function StarterCards<S extends Starter>({
  samples,
  onPick,
}: {
  samples: S[];
  onPick: (sample: S) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState(STARTER_PAGE);
  const rowRef = useRef<HTMLDivElement>(null);
  // Ask for the full page; while the row overflows, ask for one fewer. It
  // settles in at most two extra renders and cannot oscillate — the row is
  // flex-1, so its own width does not change with the count.
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    if (page > STARTER_MIN && row.scrollWidth > row.clientWidth + 1) setPage(page - 1);
  }, [page, offset, samples]);
  // A resize re-opens the question upwards: dropping a pill is a one-way
  // ratchet within a layout, so a column that grows back has to re-ask.
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
    <div className="flex items-center gap-2">
      <div ref={rowRef} className="flex flex-1 gap-2 overflow-hidden">
        {shown.map((sample) => (
          <Button
            key={sample.name}
            type="button"
            variant="outline"
            size="sm"
            className="rounded-full text-muted-foreground"
            title={sample.detail ?? sample.prompt}
            onClick={() => onPick(sample)}
          >
            <span aria-hidden="true" className="[&_svg]:size-3.5">
              {sample.icon}
            </span>
            {sample.name}
          </Button>
        ))}
      </div>
      {samples.length > page && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="rounded-full text-muted-foreground"
          title="Show other examples"
          aria-label="Show other examples"
          onClick={() => setOffset((at) => (at + page) % samples.length)}
        >
          <RefreshCwIcon />
        </Button>
      )}
    </div>
  );
}

/** A labelled block a stage's answer lands in — the shared frame for the idle
 *  slot below and each stage's filled result. */
export function AnswerBlock({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className={cn("relative", className)}>{children}</div>
    </div>
  );
}

/** The result canvas, idle. A PLACEHOLDER, not a skeleton: no shimmer, no
 *  fake rows — a framed Empty with the capability's own glyph and one line
 *  naming what arrives here. `label` repeats the filled block's heading and
 *  the slot sits in the SAME JSX position, so idle and filled are one box in
 *  two states. */
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
      <Empty className="rounded-lg border border-dashed py-10">
        <EmptyHeader>
          <EmptyMedia variant="icon">{capabilityIcon(capability)}</EmptyMedia>
          <EmptyDescription>{note}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    </AnswerBlock>
  );
}

// Re-export the field primitives the stages compose their own one-off
// controls from (the text stage's system prompt, the transcribe toggles).
export { Field, FieldContent, FieldDescription, FieldGroup, FieldLabel, FieldTitle };
