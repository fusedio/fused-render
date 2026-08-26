// The Home hero — wordmark, headline, and the prompt-first composer that
// names (haiku via /api/ai), scaffolds (POST /api/apps/new), and lands in the
// new app's claude chat. Shared by Home ("/") and the /apps hub, which is why
// it lives in the builder app rather than the shell.
import { useEffect, useRef, useState } from "react";
import { aiComplete, createApp, getHomeApps, type DefaultModel, type SessionEffort } from "@platform/lib/api";
import { navigate, navigateUrl, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { useAutoGrow } from "@platform/lib/autoGrow";
import { startersFor } from "./starterPrompts";
import logoMarkDark from "@assets/logo-black-bg-transparent.png";
import logoMarkLight from "@assets/logo-white-bg-transparent.png";
import { Select, TextArea, TextInput } from "@platform/ui/field/fields";
import { type AppAnnotation } from "@platform/lib/appAnnotation";
import { announceTasksChanged } from "@platform/lib/tasksChanged";

// The new app's page with the Claude pane open on the scaffolding turn: the
// file's ordinary explorer URL, `_side=claude` for the pane (the same hop a
// task row makes, schedule-lib.explorerUrl), and `run` so the pane's boot
// re-attaches to the live run instead of showing its landing page — with no
// session id yet there is nothing else it could adopt. `model`/`effort` ride
// along when the composer's pickers were used, so the pane's own pills open
// showing what the turn actually ran with and the NEXT turn keeps it; omitted
// when empty, since an empty param would beat the template's own detection.
export function appLandingUrl(
  entryHtml: string,
  runId: string,
  model: DefaultModel = "",
  effort: SessionEffort = "",
): string {
  const params = new URLSearchParams({ _side: "claude", run: runId });
  if (model) params.set("model", model);
  if (effort) params.set("effort", effort);
  return urlForFsPath(entryHtml, "?" + params.toString());
}

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
  NAME_NONE + ". Reply with ONLY the name — no quotes, no prose.";

// A kebab-case folder name for an app described by `prompt`: ask the AI relay
// (haiku, the server default — cheap and fast). null when the relay is down or
// answers garbage — the caller then ASKS the user for a name. It used to slug
// the prompt's own words instead, which produced folders like
// "make-me-a-tool-that" and nobody could tell that naming had failed at all.
async function suggestAppName(prompt: string): Promise<string | null> {
  try {
    const text = await aiComplete(prompt, NAME_SYSTEM_PROMPT).then((t) => t.trim());
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
// than the row had room for — `.home-composer-samples` wraps, so a narrow
// window folds the fourth chip onto its own line instead of overflowing.
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

// A glyph each, because the two pickers sit side by side with no border to tell
// them apart: the icon is what says which control you are looking at before the
// value is read — and it is what lets the labels stay bare words ("Auto",
// "high") instead of spelling their own axis out. Drawn in the composer's own weight (13px,
// 2px stroke) rather than borrowed from MenuIcons, which is tuned 1.5px for menu
// rows — a menu glyph beside these chips reads thin and unrelated.
const PICK_GLYPHS = {
  // Model — the sparkle MenuIcons uses for "new", the app's existing mark for
  // the AI doing something on your behalf.
  model: (
    <path d="M11 3.5l1.6 4.4 4.4 1.6-4.4 1.6L11 15.5 9.4 11.1 5 9.5l4.4-1.6L11 3.5zM17.5 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z" />
  ),
  // Effort — a gauge: a needle on a dial is the one figure that reads as "how
  // hard is this being pushed" without a word beside it.
  effort: (
    <>
      <path d="M4.5 17a8 8 0 1 1 15 0" />
      <path d="M12 15l3.5-4" />
    </>
  ),
};

// One borderless picker: glyph, then a native select carrying its own chevron.
// A <label> around both so the glyph is part of the control's hit area rather
// than decoration beside it — the pill has no border to aim at, so the target
// has to be the whole thing.
function ComposerPick<T extends string>({
  glyph,
  label,
  value,
  choices,
  disabled,
  onPick,
}: {
  glyph: keyof typeof PICK_GLYPHS;
  label: string;
  value: T;
  choices: { value: T; label: string }[];
  disabled: boolean;
  onPick: (next: T) => void;
}) {
  return (
    <label className="home-composer-pick">
      <span className="home-composer-pick-glyph" aria-hidden="true">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {PICK_GLYPHS[glyph]}
        </svg>
      </span>
      <Select
        className="home-composer-pick-sel"
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(e) => onPick(e.target.value as T)}
      >
        {choices.map((c) => (
          <option key={c.value} value={c.value}>
            {c.label}
          </option>
        ))}
      </Select>
    </label>
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
  const [phase, setPhase] = useState<"idle" | "naming" | "askName" | "creating">("idle");
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
  // the same ten lines `.home-composer-input`'s max-height names, and the same
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

  // The chip's detail is instructions for the CLAUDE SESSION, not something
  // the user typed — spliced in ahead of what they wrote so it reads as
  // "here's the model, here's the app I want" without the user ever
  // seeing or editing the model prose.
  const fullPrompt = () => {
    const trimmed = prompt.trim();
    return annotation ? `${annotation.detail}\n\nThe app I want: ${trimmed}` : trimmed;
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
      const res = await createAppUnderFreeName(name, fullPrompt(), model, effort);
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
        navigateUrl(appLandingUrl(res.entry_html, res.task.run_id, model, effort), { isDir: false });
      } else {
        navigate(res.entry_html, { isDir: false });
      }
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setPhase(back);
        // The row remounts on the way back; put the caret where it was so
        // Escape / a corrected name work without a click first.
        if (back === "askName") requestAnimationFrame(() => nameRef.current?.select());
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
    <div className="home-composer-wrap">
      <div className={"home-composer" + (phase === "naming" || phase === "creating" ? " is-busy" : "")}>
        {annotation && (
          <div className="home-composer-annots">
            <span className="home-composer-annot" title={annotation.detail}>
              <span className="home-composer-annot-at" aria-hidden="true">@</span>
              {annotation.name}
              <button
                type="button"
                className="home-composer-annot-x"
                aria-label={`Remove ${annotation.name} annotation`}
                disabled={busy}
                onClick={() => setAnnotation(null)}
              >
                ✕
              </button>
            </span>
          </div>
        )}
        <TextArea
          ref={inputRef}
          className="home-composer-input"
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
        <div className="home-composer-bar">
          {/* The bar's left end used to spell out ↵ / ⇧↵. Those are the two
              keystrokes every chat box on the machine already answers to, and
              the space is worth more spent on the one thing this composer could
              not say at all: WHICH Claude builds the app. The pickers
              stay mounted while a create is in flight — disabled, like the
              starter chips — and the phase text takes the space beside them
              rather than replacing them, so nothing moves when it appears. */}
          {/* Naming failed — say so and ask, rather than shipping a slug of the
              prompt. The row replaces the pickers while it is up: the name is
              the one decision left before the create, and model/effort are
              already chosen. Enter confirms, Escape backs out to the prompt. */}
          {phase === "askName" ? (
            <div className="home-composer-name" role="group" aria-label="Name your app">
              <span className="home-composer-name-why">
                Couldn't name it automatically — pick a name:
              </span>
              <TextInput
                ref={nameRef}
                className="home-composer-name-input"
                aria-label="App name"
                aria-invalid={!nameOk}
                value={nameDraft}
                spellCheck={false}
                autoComplete="off"
                // Space types a hyphen: the field wants kebab-case, so the key
                // a user reaches for between words produces the separator the
                // name needs. Done on the value (not keydown) so a pasted
                // "My App" lands as "my-app" too.
                onChange={(e) => setNameDraft(e.target.value.toLowerCase().replace(/\s+/g, "-"))}
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
              <button
                type="button"
                className="home-composer-name-cancel"
                onClick={cancelName}
              >
                Cancel
              </button>
            </div>
          ) : (
          <div className="home-composer-picks">
            <ComposerPick
              glyph="model"
              label="Model"
              value={model}
              choices={MODEL_CHOICES}
              disabled={busy}
              onPick={setModel}
            />
            <ComposerPick
              glyph="effort"
              label="Effort"
              value={effort}
              choices={EFFORT_CHOICES}
              disabled={busy}
              onPick={setEffort}
            />
            <span className="home-composer-hint">
              {phase === "naming" && "Naming your app…"}
              {phase === "creating" && "Creating the app…"}
            </span>
          </div>
          )}
          {/* In askName the send button confirms the name — same spot, same
              arrow, so the gesture that started the create finishes it. */}
          <button
            type="button"
            className="home-composer-send"
            aria-label={phase === "askName" ? "Create with this name" : "Build it"}
            title={phase === "askName" ? "Create with this name" : "Build it"}
            disabled={phase === "askName" ? !nameOk : !canSubmit}
            onClick={phase === "askName" ? confirmName : submit}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 19V5M5 12l7-7 7 7" />
            </svg>
          </button>
        </div>
      </div>
      <div className="home-composer-samples">
        {Array.from({ length: Math.min(SAMPLE_ROW, samples.length) }, (_, i) => {
          const s = samples[(sampleOffset + i) % samples.length];
          return (
            <button
              key={s.label}
              type="button"
              className="home-composer-sample"
              title={s.prompt}
              disabled={busy}
              onClick={() => setPrompt(s.prompt)}
            >
              <span className="home-composer-sample-glyph" aria-hidden="true">
                {s.glyph}
              </span>
              {s.label}
            </button>
          );
        })}
        <button
          type="button"
          className="home-composer-sample home-composer-shuffle"
          aria-label="More ideas"
          title="More ideas"
          disabled={busy}
          onClick={() => setSampleOffset((o) => (o + SAMPLE_ROW) % samples.length)}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v5h-5" />
          </svg>
        </button>
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
          <span className="deploy-muted">
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
export function HomeHero({ onCreated }: { onCreated: () => void }) {
  return (
    <header className="home-hero">
      {/* Fused mark + headline. The "App" wordmark that used to sit between
          them is gone — the sidebar entry that got you here already names the
          page — so the mark stands alone beside the tagline. Both theme
          renders are in the DOM; CSS shows the one matching data-theme. */}
      <h1 className="home-hero-brand">
        <img className="home-hero-logo home-hero-logo-dark" src={logoMarkDark} alt="" aria-hidden="true" />
        <img className="home-hero-logo home-hero-logo-light" src={logoMarkLight} alt="" aria-hidden="true" />
        <span className="home-hero-tagline">
          Build your next <span className="home-hero-accent">local app</span>
        </span>
      </h1>
      {/* The hero's only verb, prompt-first: describe the app right here
          and a named, scaffolded folder + claude session comes back. */}
      <HeroComposer onCreated={onCreated} />
    </header>
  );
}
