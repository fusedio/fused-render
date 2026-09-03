// The Home hero — wordmark, headline, and the prompt-first composer that
// names (haiku via /api/ai), scaffolds (POST /api/apps/new), and lands in the
// new app's claude chat. Shared by Home ("/") and the /apps hub, which is why
// it lives in the builder app rather than the shell.
import { useEffect, useRef, useState, type ComponentProps } from "react";
import { ArrowUp, Gauge, RefreshCw, Sparkles, X, type LucideIcon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Card } from "@platform/shadcn/ui/card";
import { Input } from "@platform/shadcn/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@platform/shadcn/ui/select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Tiny } from "@platform/ui/flow/Typography";
import {
  aiComplete,
  createApp,
  getHomeApps,
  type DefaultModel,
  type SessionEffort,
} from "@platform/lib/api";
import { navigate, navigateUrl, replaceSearch } from "@platform/lib/router";
import { appLandingUrl } from "@platform/lib/appLanding";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { startersFor } from "./starterPrompts";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { type AppAnnotation } from "@platform/lib/appAnnotation";
import { announceTasksChanged } from "@platform/lib/tasksChanged";

// The new app's page with the Claude pane open on the scaffolding turn is
// `appLandingUrl` (platform/lib/appLanding) — shared with the Migrate buttons.

// -- Prompt-first creation (the hero composer) --------------------------------

// Kebab-case whatever the model gave us into a safe app folder name:
// lowercase, [a-z0-9-] only, at most five words. Returns "" when nothing
// usable survives.
function kebabName(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .split("-")
    .filter(Boolean)
    .slice(0, 5)
    .join("-")
    .slice(0, 48);
}

// What a user-typed app name must look like: lowercase kebab-case, letters and
// digits only, no leading/trailing/double hyphens. The server's
// _app_name_error (routers/apps.py) is looser and stays authoritative; this is
// the composer holding its own names to the standard the model is asked for.
const KEBAB_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

// The refusal token. Asked for explicitly so a request that is not an app
// description gets a deterministic answer the code can act on (ask the user)
// instead of an invented brand — Haiku, given nothing concrete, will happily
// produce "flux-name-bot" and that passes every syntactic check.
const NAME_NONE = "none";

const NAME_SYSTEM_PROMPT =
  "You name software projects. Given a description of an app, reply with a " +
  "short kebab-case name for it: 2-4 lowercase words joined by hyphens, " +
  "letters and digits only. Use only words that appear in the description or " +
  "plainly describe what the app does — never invented brand words. If the " +
  "text is not a description of an app to build, reply with exactly: " +
  NAME_NONE +
  ". Reply with ONLY the name — no quotes, no prose.";

// A kebab-case folder name for an app described by `prompt`: ask the AI relay
// (haiku, the server default — cheap and fast). null when the relay is down or
// answers garbage — the caller then ASKS the user for a name. It used to slug
// the prompt's own words instead, which produced folders like
// "make-me-a-tool-that" and nobody could tell that naming had failed at all.
async function suggestAppName(prompt: string): Promise<string | null> {
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT).then((t) =>
      t.trim(),
    );
    // NAME_SYSTEM_PROMPT asks for a bare kebab-case reply; if the model added
    // prose instead (common even with "no prose" in the ask), `text` won't be
    // pure lowercase-and-hyphens. Taking the first token then would name the
    // folder after whatever word led the prose (e.g. "sure").
    if (text.toLowerCase().replace(/[.\s]+$/, "") === NAME_NONE) return null;
    if (KEBAB_RE.test(text)) {
      const name = kebabName(text);
      if (name) return name;
    }
  } catch {
    // relay down / claude missing — fall through to null
  }
  return null;
}

// The prefill offered when naming failed: "my-app-<n>", n one past the highest
// my-app-N already on the home row. Cosmetic only — createAppUnderFreeName
// suffixes on a 409 anyway — so a failed lookup just yields my-app-1. Never
// getApps(): that is the recursive workspace walk (see Apps.tsx).
async function genericAppName(): Promise<string> {
  let n = 1;
  try {
    const { apps } = await getHomeApps(50);
    for (const a of apps) {
      const m = /^my-app(?:-(\d+))?$/.exec(a.name);
      if (m) n = Math.max(n, (m[1] ? Number(m[1]) : 1) + 1);
    }
  } catch {
    // no list, no number
  }
  return `my-app-${n}`;
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
// shuffle button advances. Four rather than three because the pool is deep
// enough now (starterPrompts.tsx) that three was showing a smaller share of it
// than the row had room for. The row is ONE line always: the chips sit in a
// no-wrap strip that scrolls sideways when four of them are wider than the
// composer, with the shuffle button outside
// the strip so it stays visible however far the chips are scrolled.
const SAMPLE_ROW = 4;

// The composer's two session pickers — what the scaffolding turn runs
// with, and what the chat it lands in opens showing. Both lists are the claude
// template's own vocabulary (template.html MODELS / EFFORTS, and the server
// validates against the same sets), because these values are handed straight to
// the CLI as --model / --effort.
//
// "" is the FIRST option of each and the default: it means no flag at all, so
// the session keeps whatever the template would have detected for this project
// from its own transcripts and settings. A composer that shipped `sonnet` /
// `medium` preselected would silently override that detection for every app
// built from here, which is a stronger claim than the picker is making.
//
// The labels are BARE — "Auto", "opus", "high" — and not "Auto model" / "high
// effort": each pill's glyph names its axis, so repeating it in the text is the
// same word twice in one control. It is also what keeps two pills quiet enough
// to sit unbordered in the footer of the box rather than reading as buttons.
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

// One quiet picker: lucide glyph, then a shadcn Select whose trigger is
// UNBORDERED and unfilled at rest — the pickers are settings ON the send, not
// things to press. The glyph is inside the trigger so it shares the hit area.
//
// base-ui's Select rejects "" as an item value, so the "no flag" choice
// travels through the control as AUTO and is mapped back to "" at this
// boundary — the state, and therefore the CLI flags, never see the sentinel.
const AUTO = "auto";

function ComposerPick<T extends string>({
  icon: Icon,
  label,
  value,
  choices,
  disabled,
  onPick,
}: {
  icon: LucideIcon;
  label: string;
  value: T;
  choices: { value: T; label: string }[];
  disabled: boolean;
  onPick: (next: T) => void;
}) {
  return (
    <Select
      value={value === "" ? AUTO : value}
      disabled={disabled}
      onValueChange={(v) => onPick((v === AUTO ? "" : v) as T)}
    >
      <SelectTrigger
        size="sm"
        aria-label={label}
        className="h-7 gap-1 rounded-full border-transparent bg-transparent px-2 text-xs text-muted-foreground shadow-none hover:bg-accent hover:text-foreground focus-visible:ring-0 focus-visible:border-transparent focus-visible:bg-accent focus-visible:text-foreground dark:bg-transparent dark:hover:bg-accent [&_svg:not([class*='size-'])]:size-3.5"
      >
        <Icon className="size-3.5 shrink-0" aria-hidden="true" />
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="min-w-28">
        {choices.map((c) => (
          <SelectItem key={c.value} value={c.value === "" ? AUTO : c.value}>
            {c.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// A starter/shuffle chip: outline pill, never shrinks or wraps (the strip
// scrolls instead). Used by both the idea chips and the shuffle button.
function Chip({ className, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn("shrink-0 snap-start rounded-full text-xs text-muted-foreground hover:text-foreground", className)}
      {...props}
    />
  );
}

// The hero's prompt box — the claude.ai / v0 "what do you want to build?"
// composer. Submitting names the app (haiku via /api/ai), scaffolds it, and
// lands in the new folder's claude chat exactly like the New-app panel does.
// Exported on its own (without the wordmark) for the sidebar's "Current apps"
// + button (shell/CurrentAppsSection.tsx, D489), which opens THIS composer in a
// modal: same naming, same scaffold, same landing in the new app's chat. One
// create path, three doors.
export function HeroComposer({ onCreated }: { onCreated: () => void }) {
  const [prompt, setPrompt] = useState("");
  // The chip above the box — set by the Playground's "Build an app with this
  // AI" button (?annot=). null means no model is annotated; the composer
  // reads as a plain prompt box exactly like today.
  const [annotation, setAnnotation] = useState<AppAnnotation | null>(null);
  // Empty = "let the chat decide", the default; see MODEL_CHOICES.
  const [model, setModel] = useState<DefaultModel>("");
  const [effort, setEffort] = useState<SessionEffort>("");
  const [phase, setPhase] = useState<
    "idle" | "naming" | "askName" | "creating"
  >("idle");
  // The name the user is typing while phase === "askName" — only reached when
  // haiku could not name the app; see suggestAppName.
  const [nameDraft, setNameDraft] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  // Claude would not start for an app that WAS created — see the submit path.
  const [sessionError, setSessionError] = useState<string | null>(null);
  // Which window of SAMPLE_ROW starter chips is showing; shuffle advances it.
  const [sampleOffset, setSampleOffset] = useState(0);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  // Grow the box with its content, capped at the shared COMPOSER_MAX_LINES —
  // the same ten lines the box's max-height names, and the same
  // hook the Playground's composers use. Keyed on `prompt`, which is what makes
  // a starter chip's brief (the longest text that lands in here, and it never
  // goes through onChange) open at its full height rather than three lines with
  // the rest scrolled away.
  const { ref: inputRef } = useAutoGrow(prompt);

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
  // attached as a chip — only the briefs for what that model DOES, so the row
  // under an image model is four image apps rather than four ideas it cannot
  // help with. `startersFor` falls back to the full pool for a chip carrying no
  // capability (an older `?annot=` link).
  const samples = startersFor(annotation?.capability);
  // The window has to restart when the pool underneath it changes: an offset
  // picked in the 30-odd mixed pool is meaningless in a five-brief capability
  // slice — modulo would keep it in range but land somewhere arbitrary.
  useEffect(() => setSampleOffset(0), [annotation?.capability]);
  // ...and so does the strip's sideways scroll, on EITHER kind of change. The
  // chips are swapped inside a scroll container that keeps its offset, so a
  // row the user had scrolled to the end would show the new batch's tail
  // instead of its first idea — the one thing shuffle is for.
  const stripRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    stripRef.current?.scrollTo({ left: 0 });
  }, [sampleOffset, annotation?.capability]);

  // The chip's detail is instructions for the CLAUDE SESSION, not something
  // the user typed — spliced in ahead of what they wrote so it reads as
  // "here's the model, here's the app I want" without the user ever
  // seeing or editing the model prose.
  const fullPrompt = () => {
    const trimmed = prompt.trim();
    return annotation
      ? `${annotation.detail}\n\nThe app I want: ${trimmed}`
      : trimmed;
  };

  // Second half of a create: the app has a name, scaffold it and land in its
  // chat. Shared by the automatic path (haiku named it) and the ask path (the
  // user did), so both end up in exactly the same place. `back` is where a
  // failure returns to: the ask row keeps the name the user typed (a retry
  // from idle would re-run naming and overwrite it with a fresh my-app-N),
  // the automatic path goes back to the prompt.
  const createNamed = async (name: string, back: "idle" | "askName") => {
    setError(null);
    setSessionError(null);
    setPhase("creating");
    try {
      const res = await createAppUnderFreeName(
        name,
        fullPrompt(),
        model,
        effort,
      );
      // The folder exists from here on, so the Recent grid is stale — refresh it
      // now, since the task-error branch below stays on this page.
      onCreated();
      // And the prompt is a task now: the sidebar's Current apps section reads
      // the shared tasks store, which otherwise learns of the new row on its
      // next slow poll (up to 30s idle). One announcement, forwarded by App.tsx
      // to pokeTasks, and the app is in the sidebar before the page turns.
      if (res.task) announceTasksChanged();
      // A task error must not read as success: the FOLDER exists, only the
      // task failed. Kept as its own state so the card can say that plainly
      // and still show the error verbatim.
      if (res.task_error) {
        if (alive.current) {
          setSessionError(res.task_error);
          setPhase("idle");
        }
        return;
      }
      // Land on the app's page with Claude building it in the side pane. The
      // prompt is a task on this file now; the server ran it and waited for
      // the run id, so the pane can attach. A task whose send failed (or is
      // still spawning past the server's wait) lands on the bare page — the
      // row in the app's Tasks tab tells the rest.
      if (res.task?.run_id) {
        navigateUrl(
          appLandingUrl(res.entry_html, res.task.run_id, model, effort),
          { isDir: false },
        );
      } else {
        navigate(res.entry_html, { isDir: false });
      }
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setPhase(back);
        // The row remounts on the way back; put the caret where it was so
        // Escape / a corrected name work without a click first.
        if (back === "askName")
          requestAnimationFrame(() => nameRef.current?.select());
      }
    }
  };

  // First half: name the app. Haiku names it and creation follows at once;
  // when it cannot, the composer says so and asks for a name instead of
  // quietly shipping a slug of the prompt.
  const submit = async () => {
    if (!canSubmit) return;
    setError(null);
    setSessionError(null);
    setPhase("naming");
    const name = await suggestAppName(prompt.trim());
    if (!alive.current) return;
    if (name) {
      await createNamed(name, "idle");
      return;
    }
    const generic = await genericAppName();
    if (!alive.current) return;
    setNameDraft(generic);
    setPhase("askName");
    requestAnimationFrame(() => nameRef.current?.select());
  };

  const nameOk = KEBAB_RE.test(nameDraft);
  const confirmName = () => {
    if (phase !== "askName" || !nameOk) return;
    void createNamed(nameDraft, "askName");
  };
  const cancelName = () => {
    setPhase("idle");
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  return (
    <div className="flex w-full flex-col gap-2.5">
      <Card
        size="sm"
        className={cn(
          "gap-0 border border-border py-0 shadow-sm ring-0 transition-[border-color,opacity] focus-within:border-ring motion-reduce:transition-none",
          (phase === "naming" || phase === "creating") && "opacity-75",
        )}
      >
        {annotation && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-3">
            <Badge variant="secondary" title={annotation.detail} className="h-6 gap-1 pr-1">
              <span className="opacity-60" aria-hidden="true">
                @
              </span>
              {annotation.name}
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="size-4 rounded-full"
                aria-label={`Remove ${annotation.name} annotation`}
                disabled={busy}
                onClick={() => setAnnotation(null)}
              >
                <X className="size-3" />
              </Button>
            </Badge>
          </div>
        )}
        <Textarea
          ref={inputRef}
          // Chrome off: the card is the border. `field-sizing-fixed` because
          // useAutoGrow writes the height inline (3–10 lines; the max-height
          // here is the backstop naming the same ten lines as COMPOSER_MAX_LINES).
          className="field-sizing-fixed max-h-[230px] min-h-[83px] resize-none overflow-y-auto rounded-none border-0 bg-transparent px-3.5 pt-3.5 pb-1.5 text-sm leading-[1.5] shadow-none focus-visible:border-0 focus-visible:ring-0 disabled:bg-transparent dark:bg-transparent dark:disabled:bg-transparent"
          placeholder="What do you want to build?"
          aria-label="What do you want to build?"
          value={prompt}
          rows={1}
          disabled={busy}
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
        <div className="flex items-center justify-between gap-2.5 px-2.5 pt-1.5 pb-2.5 pl-3.5">
          {/* The pickers stay mounted while a create is in flight — disabled,
              like the starter chips — and the phase text takes the space beside
              them rather than replacing them, so nothing moves when it appears. */}
          {/* Naming failed — say so and ask, rather than shipping a slug of the
              prompt. The row replaces the pickers while it is up: the name is
              the one decision left before the create, and model/effort are
              already chosen. Enter confirms, Escape backs out to the prompt. */}
          {phase === "askName" ? (
            <div
              className="flex min-w-0 flex-1 flex-wrap items-center gap-2"
              role="group"
              aria-label="Name your app"
            >
              <span className="text-xs text-muted-foreground">
                Couldn't name it automatically — pick a name:
              </span>
              <Input
                ref={nameRef}
                className="h-7 w-44 font-mono text-xs"
                aria-label="App name"
                aria-invalid={!nameOk}
                value={nameDraft}
                spellCheck={false}
                autoComplete="off"
                // Space types a hyphen: the field wants kebab-case, so the key
                // a user reaches for between words produces the separator the
                // name needs. Done on the value (not keydown) so a pasted
                // "My App" lands as "my-app" too.
                onChange={(e) =>
                  setNameDraft(
                    e.target.value.toLowerCase().replace(/\s+/g, "-"),
                  )
                }
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    confirmName();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelName();
                  }
                }}
              />
              <Button type="button" variant="ghost" size="xs" onClick={cancelName}>
                Cancel
              </Button>
            </div>
          ) : (
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <ComposerPick
                icon={Sparkles}
                label="Model"
                value={model}
                choices={MODEL_CHOICES}
                disabled={busy}
                onPick={setModel}
              />
              <ComposerPick
                icon={Gauge}
                label="Effort"
                value={effort}
                choices={EFFORT_CHOICES}
                disabled={busy}
                onPick={setEffort}
              />
              <Tiny>
                {phase === "naming" && "Naming your app…"}
                {phase === "creating" && "Creating the app…"}
              </Tiny>
            </div>
          )}
          {/* In askName the send button confirms the name — same spot, same
              arrow, so the gesture that started the create finishes it. */}
          <Button
            type="button"
            size="icon-sm"
            className="rounded-full"
            aria-label={
              phase === "askName" ? "Create with this name" : "Build it"
            }
            title={phase === "askName" ? "Create with this name" : "Build it"}
            disabled={phase === "askName" ? !nameOk : !canSubmit}
            onClick={phase === "askName" ? confirmName : submit}
          >
            <ArrowUp aria-hidden="true" />
          </Button>
        </div>
      </Card>
      {/* Starter-idea chips under the composer — click fills the box, never
          submits. Always ONE line: the chips scroll sideways inside the strip
          when they don't fit, while the shuffle button lives outside the strip
          so it never scrolls away. Centred under the box: the strip is
          shrink-to-fit, so a row narrower than the composer sits centred and
          only goes edge-to-edge once the chips actually overflow. min-w-0 on
          the strip is load-bearing: without it a flex item's auto min-size
          keeps it at content width and the row overflows instead of scrolling. */}
      <div className="flex min-w-0 flex-nowrap items-center justify-center gap-2">
        <div
          className="flex min-w-0 flex-[0_1_auto] flex-nowrap snap-x snap-proximity gap-2 overflow-x-auto [overscroll-behavior-x:contain] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          ref={stripRef}
        >
          {Array.from(
            { length: Math.min(SAMPLE_ROW, samples.length) },
            (_, i) => {
              const s = samples[(sampleOffset + i) % samples.length];
              return (
                <Chip
                  key={s.label}
                  title={s.prompt}
                  disabled={busy}
                  onClick={() => setPrompt(s.prompt)}
                >
                  <span className="inline-flex text-muted-foreground" aria-hidden="true">
                    {s.glyph}
                  </span>
                  {s.label}
                </Chip>
              );
            },
          )}
        </div>
        <Chip
          className="px-2"
          aria-label="More ideas"
          title="More ideas"
          disabled={busy}
          onClick={() =>
            setSampleOffset((o) => (o + SAMPLE_ROW) % samples.length)
          }
        >
          <RefreshCw aria-hidden="true" />
        </Chip>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {/* THE OTHER PLACE A USER REACHES FOR CLAUDE, and the one where being
          told to install it matters most: they typed what to build. The app
          folder is already there, so the card says so rather than reading as a
          total failure — and the spawn error stays verbatim inside it, which a
          prefixed string could not do. */}
      {sessionError && (
        <TroubleCard
          what="creating the task that builds a new app"
          error={sessionError}
          facts={{ page: location.pathname + location.search }}
          onRetry={() => setSessionError(null)}
        >
          <span className="text-sm text-muted-foreground">
            The app folder was created — only the task failed.
          </span>
        </TroubleCard>
      )}
    </div>
  );
}

// The hero card itself — wordmark, headline, and the prompt composer.
// Exported so /apps can show the exact same hero above its grid; `onCreated`
// lets each page refresh its own app list once the folder exists.
// `home-hero` stays on the element as a DOM hook only — the welcome tour
// (platform/lib/tours/home.ts) anchors its first step to it; no stylesheet
// styles it any more.
export function HomeHero({ onCreated }: { onCreated: () => void }) {
  return (
    <header className="home-hero mx-auto w-full max-w-3xl px-4 pt-8 pb-6">
      {/* Fused mark + headline. Both theme renders are in the DOM; the `dark`
          variant (keyed on data-theme) shows the one matching the theme. */}
      <h1 className="mb-4 flex flex-wrap items-center justify-center gap-x-0.5 gap-y-1 text-center">
        <img className="hidden h-12 dark:block" src={logoMarkDark} alt="" aria-hidden="true" />
        <img className="block h-12 dark:hidden" src={logoMarkLight} alt="" aria-hidden="true" />
        <span className="text-xl font-medium tracking-tight text-muted-foreground">
          Build your next <span className="text-foreground">local app</span>
        </span>
      </h1>
      {/* The hero's only verb, prompt-first: describe the app right here
          and a named, scaffolded folder + claude session comes back. */}
      <HeroComposer onCreated={onCreated} />
    </header>
  );
}
