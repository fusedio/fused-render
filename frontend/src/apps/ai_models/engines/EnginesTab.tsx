// The Engines tab of /ai-models (SPEC §40, D302) — which local-model backend
// serves each capability.
//
// It lived on Preferences, and moving it is the point of this module: the
// setting is about MODELS, and every consequence of changing it is on this page
// — which cards can be loaded, what their engine tags say, and what the Local
// tab recommends. The SENTENCES live in `@apps/ai_models/lib/engines` with
// `engines.test.ts` driving them; this file is the rows.
//
// One bordered row per capability: the name, the control, the reality — and
// under them, only the lines that have something to say (a runner's note, a
// stored preference this machine is not honouring, the outcome of a switch).
import { useEffect, useState, type ReactNode } from "react";
import { getPrefs, putAiIdleUnloadMinutes, putEngineForCapability } from "@platform/lib/api";
import type { CapabilityEngine, Prefs } from "@platform/lib/api";
import { Input } from "@platform/shadcn/ui/input";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { Tiny } from "@platform/ui/flow/Typography";
import { bucketText } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";
import EngineSelect from "@apps/ai_models/engines/EngineSelect";
import { ErrorNote } from "@apps/ai_models/shared/ErrorNote";
import { Loading } from "@apps/ai_models/shared/Loading";
import {
  capabilityLabel,
  engineNote,
  ignoredWarning,
  parseAiIdleMinutes,
  servingLine,
  strandedSelection,
  switchOutcome,
} from "@apps/ai_models/lib/engines";

/** One setting row: label, control, what is actually in force, then notes.
 *  The label is a plain span (or a <label> for a native input) — for the
 *  engine picker `EngineSelect` composes its own accessible name from the
 *  label's id, see there for why `htmlFor` is the wrong tool. */
function SettingRow({
  label,
  control,
  serving,
  children,
}: {
  label: ReactNode;
  control: ReactNode;
  serving?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="border-b border-border px-4 py-2 text-sm last:border-b-0">
      <div className="flex flex-wrap items-center gap-3">
        <span className="w-40 shrink-0 font-medium">{label}</span>
        {control}
        {serving != null && <Tiny className="min-w-0 truncate">{serving}</Tiny>}
      </div>
      {children != null && <div className="mt-1.5 flex flex-col gap-1 pl-[10.75rem]">{children}</div>}
    </div>
  );
}

function Note({ className, children }: { className?: string; children: ReactNode }) {
  return <p className={cn("text-xs text-muted-foreground", className)}>{children}</p>;
}

// One capability's engine: a select holding Automatic and every backend.
// Unavailable engines stay in the menu, disabled and explained (EngineSelect).
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
  const serving = servingLine(row, auto);
  const labelId = `engine-${row.capability}-label`;

  const choose = async (code: string) => {
    if (busy || code === row.selected) return;
    setBusy(true);
    setError(null);
    try {
      const next = await putEngineForCapability(row.capability, code);
      // `row` is the row the PUT replaced — the only state the "did anything
      // move" half of the outcome can be measured against (`switchOutcome`).
      const outcome = switchOutcome(row, code, auto, next);
      onChange(next);
      setChanged(outcome);
      // The Local tab is STILL MOUNTED behind this one: anything the switch
      // moved has to reach it now.
      if (outcome) onSwitched();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label={<span id={labelId}>{label}</span>}
      control={
        <EngineSelect
          labelId={labelId}
          auto={auto}
          selected={row.selected}
          choices={row.choices}
          stranded={stranded}
          strandedLabel={row.strandedLabel}
          disabled={busy}
          onChange={choose}
        />
      }
      // What is ACTUALLY serving this capability, beside the control: the
      // trigger shows the choice, this reports reality, and they may differ.
      serving={serving}
    >
      {/* What running on the engine in force is LIKE — muted: it describes a
          backend, it does not report a problem. */}
      {note && <Note>{note}</Note>}
      {/* The one line that IS a problem — a stored preference this machine is
          not honouring — in the warning colour. */}
      {warning && <Note className={bucketText.orange}>{warning}</Note>}
      {changed && <Note>{changed === "unloaded" ? "Switched. Loaded model unloaded." : "Switched."}</Note>}
      {error && <ErrorNote>{error}</ErrorNote>}
    </SettingRow>
  );
}

// The idle-unload window (SPEC AI-13): a resident local model this machine
// hasn't used in a while gives its gigabytes back on its own. The env override
// gets the locked-control treatment: the number shown is the STORED choice, the
// field is disabled while an override is in force, and the note names both.
function AiIdleWindowRow({ prefs, onChange }: { prefs: Prefs; onChange: (p: Prefs) => void }) {
  const idle = prefs.ai_idle;
  const locked = idle.forced_by !== null;
  // Local text, not `idle.minutes` directly: the value commits on blur/Enter
  // and only resyncs from the server after a commit actually lands.
  const [value, setValue] = useState(String(idle.minutes));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValue(String(idle.minutes));
  }, [idle.minutes]);

  const commit = async () => {
    // `parseAiIdleMinutes` stands between an emptied field and a silent PUT
    // of `0`; null snaps back to the stored value rather than guessing.
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
    <SettingRow
      label={<label htmlFor="ai-idle-minutes">Idle unload</label>}
      control={
        <Input
          id="ai-idle-minutes"
          type="number"
          min={0}
          max={1440}
          className="w-24"
          value={value}
          disabled={busy || locked}
          onChange={(e) => setValue(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
      }
      serving={
        idle.effective_minutes === 0
          ? "Never unloads on its own."
          : `Unloads an idle model after ${idle.effective_minutes} min.`
      }
    >
      <Note>Minutes before an unused model is unloaded. 0 = never.</Note>
      {locked && (
        <Note>
          Locked by <code className="font-mono">FUSED_RENDER_AI_IDLE_MINUTES={idle.forced_by}</code> for this
          process; the value above applies once the variable is removed.
        </Note>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </SettingRow>
  );
}

/** The tab's whole content: one row per capability, over its own copy of prefs.
 *  It fetches `/api/prefs` itself: this is the only thing on /ai-models that
 *  reads a preference, and the tab is not mounted until it is selected. */
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
    <div className="flex flex-col gap-4">
      {error && <ErrorNote>{error}</ErrorNote>}
      {!prefs && !error && <Loading rows={3} label="Loading engines" />}
      {prefs && (
        // No heading and no intro paragraph: the page head already says what
        // this tab is, and what the first option means lives on the Automatic
        // option itself (`EngineSelect`).
        <EntityList>
          {prefs.engines.capabilities.map((row) => (
            <CapabilityEngineRow
              key={row.capability}
              row={row}
              auto={prefs.engines.auto}
              onChange={setPrefs}
              onSwitched={onSwitched}
            />
          ))}
          <AiIdleWindowRow prefs={prefs} onChange={setPrefs} />
        </EntityList>
      )}
    </div>
  );
}
