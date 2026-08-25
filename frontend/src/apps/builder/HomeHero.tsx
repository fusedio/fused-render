// The /apps hero: the Fused mark, one headline, and the prompt composer — the
// claude.ai / v0 "what do you want to build?" box. Submitting names the app
// (haiku via /api/ai), scaffolds it, and lands in the new folder's claude chat
// exactly like the New-app panel does. Built on shadcn primitives; the page's
// only styling is Tailwind utilities on the stock palette.
import { useEffect, useRef, useState } from "react";
import { ArrowUpIcon, GaugeIcon, RefreshCwIcon, SparklesIcon, XIcon } from "lucide-react";
import { aiComplete, createApp, type DefaultModel, type SessionEffort } from "@platform/lib/api";
import { navigate, navigateUrl, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { startersFor } from "./starterPrompts";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { type AppAnnotation } from "@platform/lib/appAnnotation";
import { Alert, AlertDescription, AlertTitle } from "@platform/shadcn/ui/alert";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@platform/shadcn/ui/input-group";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@platform/shadcn/ui/select";
import { Spinner } from "@platform/shadcn/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@platform/shadcn/ui/tooltip";

// The claude-chat URL a freshly scaffolded app lands in (`_mode=claude`, the
// run to attach to, and the session flags the composer chose).
export function claudeChatUrl(
  appDir: string,
  runId: string,
  model: DefaultModel = "",
  effort: SessionEffort = "",
): string {
  const params = new URLSearchParams({ _mode: "claude", run: runId });
  if (model) params.set("model", model);
  if (effort) params.set("effort", effort);
  return urlForFsPath(appDir.replace(/\/+$/, ""), "?" + params.toString());
}

// "Build me a habit tracker" → "build-me-a-habit-tracker" — the folder-name
// fallback when the model has no better idea. Max five words, 48 chars.
function kebabName(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .split("-")
      .filter(Boolean)
      .slice(0, 5)
      .join("-")
      .slice(0, 48) || "my-app"
  );
}

const NAME_SYSTEM_PROMPT =
  "You name software projects. Given a description of an app, reply with a " +
  "short kebab-case name for it: 2-4 lowercase words joined by hyphens, " +
  "letters and digits only. Reply with ONLY the name — no quotes, no prose.";

// Ask the fast model for a name; fall back to slugging the prompt itself.
async function suggestAppName(prompt: string): Promise<string> {
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT).then((t) => t.trim());
    // NAME_SYSTEM_PROMPT asks for a bare kebab-case reply; if the model added
    // prose instead (common even with "no prose" in the ask), `text` won't be
    // pure lowercase-and-hyphens. Taking the first token then would name the
    // folder after whatever word led the prose (e.g. "sure") instead of
    // falling through to the actual prompt slug below.
    if (/^[a-z0-9]+(-[a-z0-9]+)*$/.test(text)) {
      const name = kebabName(text);
      if (name !== "my-app") return name;
    }
  } catch {
    // relay down / claude missing: the slug fallback below still works
  }
  return kebabName(prompt);
}

// Create the app under a collision-proof name: on 409 retry with -2, -3, …
// Any other failure propagates.
async function createAppUnderFreeName(
  name: string,
  prompt: string,
  model: DefaultModel,
  effort: SessionEffort,
) {
  for (let i = 1; ; i++) {
    const attempt = i === 1 ? name : `${name}-${i}`;
    try {
      return await createApp(attempt, prompt, model, effort);
    } catch (e) {
      if ((e as { status?: number }).status !== 409 || i >= 20) throw e;
    }
  }
}

// How many starter chips the row shows at once, and therefore how far the
// shuffle button advances. The pool is deep enough (starterPrompts.tsx) that
// four is still a small share of it; the row wraps on a narrow window.
const SAMPLE_ROW = 4;

// The composer's two session pickers — what the scaffolding turn runs with,
// and what the chat it lands in opens showing. Both lists are the claude
// template's own vocabulary (template.html MODELS / EFFORTS, and the server
// validates against the same sets), because these values are handed straight
// to the CLI as --model / --effort.
//
// "" is the default and means no flag at all, so the session keeps whatever the
// template would have detected for this project from its own transcripts and
// settings. A composer that shipped `sonnet` / `medium` preselected would
// silently override that detection for every app built from here.
//
// The Select carries AUTO in place of "" — a select's empty string is its
// placeholder sentinel, not a value a user can pick back — and the two are
// mapped at the edge (fromPick / toPick).
const AUTO = "auto";
const fromPick = <T extends string>(v: string | null): T => (v === null || v === AUTO ? "" : v) as T;
const toPick = (v: string): string => (v === "" ? AUTO : v);

const MODEL_CHOICES: { value: DefaultModel; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "fable", label: "fable" },
  { value: "opus", label: "opus" },
  { value: "sonnet", label: "sonnet" },
  { value: "haiku", label: "haiku" },
];

const EFFORT_CHOICES: { value: SessionEffort; label: string }[] = [
  { value: "", label: "Auto" },
  { value: "low", label: "low" },
  { value: "medium", label: "medium" },
  { value: "high", label: "high" },
  { value: "xhigh", label: "xhigh" },
  { value: "max", label: "max" },
];

// One quiet picker in the composer's footer: an icon naming the axis, then the
// bare value. Borderless (ghost) so two of them sit in the bar as controls of
// the box rather than as buttons competing with Send.
function ComposerPick<T extends string>({
  icon: Icon,
  label,
  value,
  choices,
  disabled,
  onPick,
}: {
  icon: typeof SparklesIcon;
  label: string;
  value: T;
  choices: { value: T; label: string }[];
  disabled: boolean;
  onPick: (next: T) => void;
}) {
  const items = choices.map((c) => ({ value: toPick(c.value), label: c.label }));
  return (
    <Select
      items={items}
      value={toPick(value)}
      disabled={disabled}
      onValueChange={(v) => onPick(fromPick<T>(v as string | null))}
    >
      <SelectTrigger
        size="sm"
        aria-label={label}
        className="border-transparent bg-transparent text-muted-foreground shadow-none hover:text-foreground dark:bg-transparent dark:hover:bg-accent"
      >
        <Icon />
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {items.map((c) => (
            <SelectItem key={c.value} value={c.value}>
              {c.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}

function HeroComposer({ onCreated }: { onCreated: () => void }) {
  const [prompt, setPrompt] = useState("");
  // The chip above the box — set by the Playground's "Build an app with this
  // AI" button (?annot=). null means no model is annotated.
  const [annotation, setAnnotation] = useState<AppAnnotation | null>(null);
  // Empty = "let the chat decide", the default; see MODEL_CHOICES.
  const [model, setModel] = useState<DefaultModel>("");
  const [effort, setEffort] = useState<SessionEffort>("");
  const [phase, setPhase] = useState<"idle" | "naming" | "creating">("idle");
  const [error, setError] = useState<string | null>(null);
  // Claude would not start for an app that WAS created — see the submit path.
  const [sessionError, setSessionError] = useState<string | null>(null);
  // Which window of SAMPLE_ROW starter chips is showing; shuffle advances it.
  const [sampleOffset, setSampleOffset] = useState(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  // A `?annot=` in the URL pre-fills the chip — the Playground's "Build an
  // app with this AI" hands its model + tuned settings through here as a
  // JSON `AppAnnotation`, encoded rather than dumped into the prompt text.
  // Consumed once and removed (replaceSearch, no history entry): an annot
  // that survived in the URL would reappear on the next mount.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const raw = params.get("annot");
    if (!raw) return;
    try {
      setAnnotation(JSON.parse(raw) as AppAnnotation);
    } catch {
      // Malformed/tampered param — drop it silently rather than crash the
      // composer over a URL someone hand-edited.
    }
    params.delete("annot");
    const search = params.toString();
    replaceSearch(location.pathname + (search ? "?" + search : ""));
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  const busy = phase !== "idle";
  const canSubmit = prompt.trim().length > 0 && !busy;

  // The starters on offer right now: the whole mixed pool, or — once a model is
  // attached as a chip — only the briefs for what that model DOES. The window
  // restarts when the pool underneath it changes.
  const samples = startersFor(annotation?.capability);
  useEffect(() => setSampleOffset(0), [annotation?.capability]);

  const submit = async () => {
    if (!canSubmit) return;
    const trimmed = prompt.trim();
    // The chip's detail is instructions for the CLAUDE SESSION, not something
    // the user typed — spliced in ahead of what they wrote.
    const full = annotation ? `${annotation.detail}\n\nThe app I want: ${trimmed}` : trimmed;
    setError(null);
    setSessionError(null);
    setPhase("naming");
    try {
      const name = await suggestAppName(trimmed);
      if (!alive.current) return;
      setPhase("creating");
      const res = await createAppUnderFreeName(name, full, model, effort);
      // The folder exists from here on, so the grid is stale — refresh it now,
      // since the session-error branch below stays on this page.
      onCreated();
      // Same landing logic as NewAppPanel: a session error must not read as
      // success, and a live run means the claude chat is the right landing.
      if (res.session_error) {
        if (alive.current) {
          setSessionError(res.session_error);
          setPhase("idle");
        }
        return;
      }
      if (res.run_id) navigateUrl(claudeChatUrl(res.path, res.run_id, model, effort), { isDir: true });
      else navigate(res.entry_html, { isDir: false });
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setPhase("idle");
      }
    }
  };

  return (
    <div className="flex w-full max-w-2xl flex-col gap-4">
      <InputGroup className={busy ? "opacity-70" : undefined}>
        {annotation && (
          <InputGroupAddon align="block-start">
            <Badge variant="secondary" className="gap-1 pr-1" title={annotation.detail}>
              <span className="text-muted-foreground">@</span>
              {annotation.name}
              <button
                type="button"
                aria-label={`Remove ${annotation.name} annotation`}
                disabled={busy}
                onClick={() => setAnnotation(null)}
                className="ml-0.5 inline-flex size-4 items-center justify-center rounded-sm text-muted-foreground hover:bg-foreground/10 hover:text-foreground disabled:opacity-50"
              >
                <XIcon className="size-3" />
              </button>
            </Badge>
          </InputGroupAddon>
        )}
        <InputGroupTextarea
          ref={inputRef}
          placeholder="What do you want to build?"
          aria-label="What do you want to build?"
          value={prompt}
          rows={2}
          disabled={busy}
          // field-sizing grows the box with its content; the cap keeps a long
          // starter brief from pushing the toolbar below the fold.
          className="max-h-64 min-h-16 text-base md:text-sm"
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            // Enter submits (the composer is a one-shot prompt, not a
            // document); Shift+Enter keeps the newline for longer briefs.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <InputGroupAddon align="block-end" className="justify-between gap-2">
          <div className="flex min-w-0 items-center gap-0.5">
            <ComposerPick
              icon={SparklesIcon}
              label="Model"
              value={model}
              choices={MODEL_CHOICES}
              disabled={busy}
              onPick={setModel}
            />
            <ComposerPick
              icon={GaugeIcon}
              label="Effort"
              value={effort}
              choices={EFFORT_CHOICES}
              disabled={busy}
              onPick={setEffort}
            />
            {busy && (
              <span className="ml-2 flex items-center gap-1.5 text-xs font-normal text-muted-foreground">
                <Spinner />
                {phase === "naming" ? "Naming your app…" : "Creating the app…"}
              </span>
            )}
          </div>
          <InputGroupButton
            size="icon-sm"
            variant="default"
            aria-label="Build it"
            disabled={!canSubmit}
            onClick={submit}
            className="rounded-full"
          >
            <ArrowUpIcon />
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>

      <div className="flex flex-wrap items-center justify-center gap-2">
        {Array.from({ length: Math.min(SAMPLE_ROW, samples.length) }, (_, i) => {
          const s = samples[(sampleOffset + i) % samples.length];
          return (
            <Button
              key={s.label}
              type="button"
              variant="outline"
              size="sm"
              title={s.prompt}
              disabled={busy}
              onClick={() => setPrompt(s.prompt)}
              className="rounded-full font-normal text-muted-foreground hover:text-foreground"
            >
              <span data-icon="inline-start" className="flex">
                {s.glyph}
              </span>
              {s.label}
            </Button>
          );
        })}
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="More ideas"
                disabled={busy}
                onClick={() => setSampleOffset((o) => (o + SAMPLE_ROW) % samples.length)}
                className="rounded-full text-muted-foreground"
              />
            }
          >
            <RefreshCwIcon />
          </TooltipTrigger>
          <TooltipContent>More ideas</TooltipContent>
        </Tooltip>
      </div>

      {error && (
        <Alert variant="destructive" className="text-left">
          <AlertTitle>Could not create the app</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {/* The folder exists; only the session failed. The card says so plainly
          and keeps the spawn error verbatim. */}
      {sessionError && (
        <div className="text-left">
          <TroubleCard
            what="starting a Claude session to build a new app"
            error={sessionError}
            facts={{ page: location.pathname + location.search }}
            onRetry={() => setSessionError(null)}
          >
            <span className="text-sm text-muted-foreground">
              The app folder was created — only the session failed.
            </span>
          </TroubleCard>
        </div>
      )}
    </div>
  );
}

// The hero itself — mark, headline, composer. `onCreated` lets the page
// refresh its grid once the folder exists.
export function HomeHero({ onCreated }: { onCreated: () => void }) {
  return (
    <header className="flex flex-col items-center gap-6 pt-6 text-center">
      {/* Both theme renders are in the DOM; the `dark` variant (tailwind.css —
          this app's default theme, `data-theme="light"` opts out) shows one. */}
      <img src={logoMarkDark} alt="" aria-hidden="true" className="hidden h-9 w-auto dark:block" />
      <img src={logoMarkLight} alt="" aria-hidden="true" className="block h-9 w-auto dark:hidden" />
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight text-balance">
          Build your next local app
        </h1>
        <p className="text-sm text-muted-foreground text-balance">
          Describe it, and a scaffolded folder with a Claude session comes back.
        </p>
      </div>
      <HeroComposer onCreated={onCreated} />
    </header>
  );
}
