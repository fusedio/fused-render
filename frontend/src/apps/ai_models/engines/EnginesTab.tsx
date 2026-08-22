// The Engines tab of /ai-models (SPEC §40, D302) — which local-model backend
// serves each capability.
//
// **It lived on Preferences, and moving it is the point of this module.** The
// setting is about MODELS, and every consequence of changing it is on this
// page: which cards can be loaded, what their engine tags say, and what
// Discover suggests. On the settings page a user had to know that "Inference
// engines" was the answer to "why can't I load this?" — a question they were
// asking with the unloadable card in front of them, two navigations away from
// the control. Here the control and its consequences are one page apart by a
// tab click, and every sentence that used to read "switch it in Preferences →
// Inference engines" now points at a tab beside the one you are on.
//
// A TAB rather than a panel above the listing: it is a settings surface, and
// stacking it on top of the grid would put a second heading hierarchy in front
// of the section headings the Local tab just gained.
//
// The rendering is four lines of JSX per row; the SENTENCES are where this can
// be wrong, and they live in `@apps/ai_models/lib/engines` with `engines.test.ts` driving
// them. Not one of them changed in the move. What the move DID add there is
// `switchOutcome`: on Preferences the consequences of a switch were on another
// page and arriving here refetched them, and now they are the tab next door.
//
// **The `.prefs-*` classes did NOT survive the move, and the argument for
// keeping them did not survive contact with the rendered page.** It was that a
// switch which unloads a model should not look like one more thing on an
// inventory page. What it produced was three bare native `<select>`s adrift on
// an empty page, each under a heading and beside a repeated "Engine" label,
// with its resolved status stranded on a line below — a form somebody forgot to
// lay out, in an app where the two tabs beside it are made of cards. The
// setting is not made less consequential by sharing the page's vocabulary; it
// is made findable. One card per capability, one row inside it: the name, the
// control, the reality.
import { useEffect, useState } from "react";
import { getPrefs, putAiIdleUnloadMinutes, putEngineForCapability } from "@platform/lib/api";
import type { CapabilityEngine, Prefs } from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import {
  capabilityLabel,
  choiceReason,
  engineNote,
  ignoredWarning,
  parseAiIdleMinutes,
  servingLine,
  strandedSelection,
  switchOutcome,
} from "@apps/ai_models/lib/engines";

// One capability's engine: a <select> holding Automatic and every backend.
//
// A dropdown rather than a radio list, so that a machine with three engines for
// one capability is one line high instead of four — and so this reads like
// every other choice-of-several in this app's settings, which are all selects.
//
// **The reason an unavailable engine cannot be picked is folded into its own
// option label**, which is the whole difficulty a dropdown has here and the
// reason this was radios first. A greyed-out radio carries its explanation
// beside it permanently; a disabled <option> is invisible until the menu is
// opened, and its `title` is not reliably shown at all. So the sentence — the
// registry's own, which the page cannot synthesise — goes in the text: "MLX
// Whisper (Apple Silicon) — needs Apple Silicon (this is windows/amd64)".
// State that would otherwise need a line of prose beside the control goes into
// the option labels, so the control still explains itself when it is read.
//
// Unavailable engines stay in the menu, disabled. Hidden, a Windows user would
// have no way to learn that the MLX path exists and why it is not for them —
// and a stored preference for one is what the select still SHOWS as its value,
// because "your choice, and why it is not in force" is exactly what the muted
// lines underneath go on to explain. It is the same rule the cards on the Local
// tab now follow for their own controls: disabled and explained, never absent.
function CapabilityEngineRow({
  row,
  auto,
  onChange,
  onSwitched,
}: {
  row: CapabilityEngine;
  auto: string;
  onChange: (p: Prefs) => void;
  /** A switch that changed something the rest of the page is showing. */
  onSwitched: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState<"switched" | "unloaded" | null>(null);
  const warning = ignoredWarning(row);
  const note = engineNote(row);
  const stranded = strandedSelection(row, auto);
  const label = capabilityLabel(row.capability);

  const choose = async (code: string) => {
    if (busy || code === row.selected) return;
    setBusy(true);
    setError(null);
    try {
      const next = await putEngineForCapability(row.capability, code);
      // `row` is still the row this closure captured, i.e. the one the PUT
      // replaced — which is the only state the "did anything move" half of the
      // outcome can be measured against. Read `switchOutcome` for what each of
      // the two halves is answering and who answers it.
      const outcome = switchOutcome(row, code, auto, next);
      onChange(next);
      setChanged(outcome);
      // The Local tab is one tab click away and is STILL MOUNTED behind this
      // one: its listing was fetched under the preference that just changed, so
      // anything the switch moved has to reach it now. Nothing else would tell
      // it — this page no longer remounts on the way here (it was a Preferences
      // tab, and the navigation back is what used to refetch).
      if (outcome) onSwitched();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cc-mdcard am-engine-card">
      {/* Capability, control, reality — one row, in that order. The three used
          to be three stacked blocks, which is what made "Engine" a visible
          label at all: a select on its own line has to say what it is for.
          Beside the capability's own name it does not, and the repeated word
          down a column of three was the loudest thing on the tab. The name
          IS the label, so it is a <label> and the select has no other. */}
      <div className="am-engine-row">
        <label className="am-engine-cap" htmlFor={`engine-${row.capability}`}>
          {label}
        </label>
        <select
          id={`engine-${row.capability}`}
          className="field-control am-engine-select"
          value={row.selected}
          disabled={busy}
          onChange={(e) => choose(e.target.value)}
        >
          {/* First, and the only option with no engine behind it. */}
          <option value={auto}>Automatic</option>
          {/* A stored engine that is not one of this capability's options,
              shown so the control is not BLANK: a <select> whose value matches
              no option renders empty, not as its first row. Disabled, because it
              cannot be re-picked — and above the real choices rather than among
              them, since it is the current value and not an alternative.

              The copy says only what `strandedSelection` can establish. It read
              "no longer available in this version", which is a claim about a
              WITHDRAWN engine and is false for the other value that lands here:
              a registered engine belonging to a different capability
              (`{"text-generation": "mlx-whisper"}` in a hand-edited prefs.json)
              is neither withdrawn nor unavailable, and the page cannot tell the
              two apart from this payload. WHICH of the two it is comes from
              `ignoredReason`, printed verbatim by `ignoredWarning` on the line
              below — so the pair still says everything, with each half saying
              only what it knows. */}
          {stranded && (
            <option value={stranded} disabled>
              {stranded} — not one of this capability's engines
            </option>
          )}
          {row.choices.map((choice) => {
            // Null for an engine that CAN be picked, which is the whole of what
            // this adds to a label: what a backend is LIKE ("transcribes on the
            // GPU") is the line under the row, where it can be read without
            // opening the menu. What is left is the registry's sentence about
            // why a greyed-out option is greyed out, and in a menu the only
            // place it can be read is here.
            const reason = choiceReason(choice);
            return (
              <option key={choice.code} value={choice.code} disabled={busy || !choice.available}>
                {choice.label}
                {reason ? ` — ${reason}` : ""}
              </option>
            );
          })}
        </select>
        {/* What is ACTUALLY serving this capability, beside the control rather
            than under it: the select shows the choice, this reports reality,
            and they are allowed to differ — which only reads as a pair when
            they are on one line. */}
        <span className="am-engine-serving">{servingLine(row)}</span>
      </div>
      {/* What running on the engine in force is LIKE — the memory ceiling on
          MLX FLUX, MLX Whisper's GPU speed. It sat over the Discover tab's
          capability sections, which was the wrong page twice: three of six
          runners have a note, so those sections came out blotchy and the
          sentences read as noise; and the FLUX one is not a fact but a
          CAUTION — it is what tells somebody on a 16GB Mac to move back to
          Diffusers, and it was two tabs away from the control that does it.
          Here it sits under the select it is about.

          MUTED, in the same line as the warning below it rather than in
          `--warning` orange: none of these reports a problem, they describe
          what a backend is like, and an orange paragraph under a control the
          user has not touched reads as an error they caused. */}
      {note && <div className="am-engine-note">{note}</div>}
      {warning && <div className="am-engine-note">{warning}</div>}
      {/* The consequence, in four words at most. It stays because an unload is
          a real thing that just happened to the user's machine and nothing else
          on screen would report it — but the paragraph explaining WHY the model
          was unloaded and how suggestion lists work was an essay after picking
          a menu item.

          TWO sentences because there are two outcomes, and the shorter one is
          not a weaker version of the longer: "Switched." is the whole truth
          when the engine moved and no model was resident to lose. */}
      {changed && (
        <div className="am-engine-note">
          {changed === "unloaded" ? "Switched. Loaded model unloaded." : "Switched."}
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// The idle-unload window (SPEC AI-13): a resident local model this machine
// hasn't used in a while gives its gigabytes back on its own. A card below
// the per-capability rows rather than a fourth column inside them — it is not
// about any one capability, it is a global number the reaper reads once per
// tick.
//
// The env override gets the SAME locked-control treatment as the call log's
// retention window (Preferences.tsx): the number shown is still the STORED
// choice (a PUT round-trips it, and it applies once the variable is
// removed), the field is disabled while an override is genuinely in force,
// and the muted line names both what is actually happening and what is
// forcing it.
function AiIdleWindowCard({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const idle = prefs.ai_idle;
  const locked = idle.forced_by !== null;
  // Local text, not `idle.minutes` directly: a bare number input has to let
  // you clear the field and type a new one without every keystroke racing a
  // PUT, so the value commits on blur/Enter and this only resyncs from the
  // server after a commit actually lands (see the effect below).
  const [value, setValue] = useState(String(idle.minutes));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(String(idle.minutes));
  }, [idle.minutes]);

  const commit = async () => {
    // `parseAiIdleMinutes` is what stands between an empty/whitespace field
    // (the ordinary intermediate state of editing a number input) and a
    // silent PUT of `0` — `Number("")` is `0`, and this input has no other
    // guard against it. `null` covers that case and every other invalid one
    // the same way: snap back to the stored value rather than guess.
    const parsed = parseAiIdleMinutes(value);
    if (parsed === null) {
      setValue(String(idle.minutes));
      return;
    }
    if (parsed === idle.minutes) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putAiIdleUnloadMinutes(parsed));
    } catch (e) {
      setError((e as Error).message);
      setValue(String(idle.minutes));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cc-mdcard am-engine-card">
      <div className="am-engine-row">
        <label className="am-engine-cap" htmlFor="ai-idle-minutes">
          Idle unload
        </label>
        <input
          id="ai-idle-minutes"
          type="number"
          min={0}
          max={1440}
          className="field-control am-engine-select"
          value={value}
          disabled={busy || locked}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        <span className="am-engine-serving">
          {idle.effective_minutes === 0
            ? "Never unloads on its own."
            : `Unloads an idle model after ${idle.effective_minutes} min.`}
        </span>
      </div>
      <p className="am-engine-note">Minutes a resident model may sit unused before it is unloaded automatically. 0 = never.</p>
      {locked && (
        <p className="am-engine-note">
          Locked by <code>FUSED_RENDER_AI_IDLE_MINUTES={idle.forced_by}</code> for this process;
          the value above applies once the variable is removed.
        </p>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

/** The tab's whole content: one row per capability, over its own copy of prefs.
 *
 *  It fetches `/api/prefs` itself rather than being handed them, because this is
 *  the only thing on /ai-models that reads a preference — threading a prefs
 *  load through the page would make every visit to the Local tab pay for a
 *  request nothing on it uses. The tab is not mounted until it is selected, so
 *  the fetch happens on the click.
 */
export default function EnginesTab({ onSwitched }: { onSwitched: () => void }) {
  const [prefs, setPrefs] = useState<Prefs | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getPrefs()
      .then((p) => alive && setPrefs(p))
      .catch((e) => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="am-engines">
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!prefs && !error && <SkeletonLines rows={3} label="Loading engines" />}
      {prefs && (
        <>
          {/* ONE line, and no heading. The page's own head already says "AI
              Models" over "Which backend runs each kind of local model", so an
              <h2> reading "Inference engines" above a paragraph reading "Which
              backend runs local models" was the tab restating its own chrome
              twice before saying anything. What cannot be inferred from three
              labelled selects is what the first option in each of them means,
              and that is all this says. */}
          <p className="am-engines-note">
            <b>Automatic</b> picks the best engine this machine can run.
          </p>
          {prefs.engines.capabilities.map((row) => (
            <CapabilityEngineRow
              key={row.capability}
              row={row}
              auto={prefs.engines.auto}
              onChange={setPrefs}
              onSwitched={onSwitched}
            />
          ))}
          <AiIdleWindowCard prefs={prefs} onChange={setPrefs} />
        </>
      )}
    </div>
  );
}
