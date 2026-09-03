import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { CheckIcon, TriangleAlertIcon } from "lucide-react";
import { commitImport, importTemplates } from "@platform/lib/api";
import type { ImportItem, ImportResolution, ImportStageResult } from "@platform/lib/api";
import { cn } from "@platform/lib/utils";
import { TemplatesDialog } from "@shell/templates/TemplatesDialog";
import { FilterGroup, WarnText } from "@shell/templates/chips";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Checkbox } from "@platform/shadcn/ui/checkbox";
import { Field, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Label } from "@platform/shadcn/ui/label";
import { Identifier, Muted, Tiny } from "@platform/ui/flow/Typography";

type WizardStep = "choose" | "manifest" | "done";

const STEPS: { id: WizardStep; label: string }[] = [
  { id: "choose", label: "Choose zip" },
  { id: "manifest", label: "Review" },
  { id: "done", label: "Done" },
];

// One binding chip in step 2. "custom" = user-added via "+ add"; the other
// statuses come from the staging response's recommendedKeys.
type ChipStatus = "new" | "already-bound" | "disabled" | "custom";
interface BindingChip {
  key: string;
  status: ChipStatus;
  on: boolean;
}

export function ImportWizard({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void;
}) {
  const [step, setStep] = useState<WizardStep>("choose");
  const [staged, setStaged] = useState<ImportStageResult | null>(null);
  // Per-item resolution for CONFLICTING valid items; non-conflicting valid
  // items are imported implicitly (see commit below).
  const [resolutions, setResolutions] = useState<Record<string, ImportResolution>>({});
  // Author-recommended binding chips, keyed by ORIGINAL staged name.
  const [chips, setChips] = useState<Record<string, BindingChip[]>>({});
  const [applyRecs, setApplyRecs] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof commitImport>> | null>(null);

  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  const formId = useId();
  const fileInputId = useId();

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const res = await importTemplates(file);
      if (!alive.current) return;
      setStaged(res);
      // Default every conflicting valid item to the safe "skip".
      const init: Record<string, ImportResolution> = {};
      for (const it of res.items) if (it.valid && it.conflictsExisting) init[it.name] = "skip";
      setResolutions(init);
      // Seed binding chips from the author's recommendations: "new" keys are
      // accepted by default, "disabled" keys are off (the user turned that
      // extension off locally). "already-bound" keys render inert and are
      // never sent — EXCEPT under keep-both, where the import lands under a
      // new name so "already bound" no longer holds; they seed ON so they
      // default to binding when that resolution unlocks them.
      const chipInit: Record<string, BindingChip[]> = {};
      for (const it of res.items) {
        if (it.valid && it.recommendedKeys && it.recommendedKeys.length > 0) {
          chipInit[it.name] = it.recommendedKeys.map((r) => ({
            key: r.key,
            status: r.status,
            on: r.status !== "disabled",
          }));
        }
      }
      setChips(chipInit);
      setApplyRecs(true);
      setStep("manifest");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const isSkipped = (it: ImportItem) =>
    it.conflictsExisting && (resolutions[it.name] ?? "skip") === "skip";

  const isKeepBoth = (it: ImportItem) =>
    it.conflictsExisting && resolutions[it.name] === "keep-both";

  // Keys to bind, per ORIGINAL staged name: ON chips of non-skipped templates.
  // "already-bound" chips are excluded (a no-op server-side anyway) UNLESS the
  // item resolves keep-both — the renamed copy isn't bound yet, so those keys
  // become real, sendable bindings.
  const activeBindings = (): Record<string, string[]> => {
    const out: Record<string, string[]> = {};
    if (!staged || !applyRecs) return out;
    for (const it of staged.items) {
      if (!it.valid || isSkipped(it)) continue;
      const keepBoth = isKeepBoth(it);
      const keys = (chips[it.name] ?? [])
        .filter((c) => c.on && (keepBoth || c.status !== "already-bound"))
        .map((c) => c.key);
      if (keys.length > 0) out[it.name] = keys;
    }
    return out;
  };

  const doCommit = async () => {
    if (!staged) return;
    setBusy(true);
    setError(null);
    try {
      // Valid non-conflicting items have no existing folder, so "overwrite"
      // simply lands them at their own name; conflicting items use the user's
      // pick (defaulting to skip). Items with no entry default to skip
      // server-side (§2.7), so we must name every item we want imported.
      const payload: Record<string, ImportResolution> = {};
      for (const it of staged.items) {
        if (!it.valid) continue;
        payload[it.name] = it.conflictsExisting ? resolutions[it.name] ?? "skip" : "overwrite";
      }
      const bindings = activeBindings();
      const res = await commitImport(
        staged.importId,
        payload,
        Object.keys(bindings).length > 0 ? bindings : undefined,
      );
      // The import already landed server-side — refresh the parent even if this
      // wizard unmounted mid-commit. The result/done screen is dialog-local, so
      // it stays alive-guarded.
      onImported();
      if (!alive.current) return;
      setResult(res);
      setStep("done");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const setRes = (name: string, r: ImportResolution) =>
    setResolutions((prev) => ({ ...prev, [name]: r }));

  const toggleChip = (name: string, key: string) =>
    setChips((prev) => ({
      ...prev,
      [name]: (prev[name] ?? []).map((c) => (c.key === key ? { ...c, on: !c.on } : c)),
    }));

  const addChip = (name: string, key: string) =>
    setChips((prev) =>
      (prev[name] ?? []).some((c) => c.key === key)
        ? prev
        : { ...prev, [name]: [...(prev[name] ?? []), { key, status: "custom", on: true }] },
    );

  const validCount = staged?.items.filter((i) => i.valid).length ?? 0;
  const hasRecs = staged?.items.some((i) => i.valid && (chips[i.name]?.length ?? 0) > 0) ?? false;
  const importCount = staged?.items.filter((i) => i.valid && !isSkipped(i)).length ?? 0;
  const bindingCount = Object.values(activeBindings()).reduce((n, keys) => n + keys.length, 0);

  let footer: ReactNode = null;
  if (step === "choose") {
    footer = (
      <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
        Cancel
      </Button>
    );
  } else if (step === "manifest" && staged) {
    footer = (
      <>
        <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" form={formId} size="sm" disabled={busy || validCount === 0}>
          {busy
            ? "Importing…"
            : hasRecs
              ? `Import ${importCount} template${importCount === 1 ? "" : "s"}` +
                (bindingCount > 0 ? ` · ${bindingCount} binding${bindingCount === 1 ? "" : "s"}` : "")
              : "Import"}
        </Button>
      </>
    );
  } else if (step === "done" && result) {
    footer = (
      <Button type="button" size="sm" onClick={onClose}>
        Done
      </Button>
    );
  }

  return (
    <TemplatesDialog title="Import templates" onClose={onClose} busy={busy} footer={footer}>
      <Stepper current={step} />

      {step === "choose" && (
        <>
          <Muted>
            Choose a <Identifier>.zip</Identifier> of template folders. Each top-level folder with a{" "}
            <Identifier>template.html</Identifier> is a template. The registry is never imported
            (folders only).
          </Muted>
          <Field>
            <FieldLabel htmlFor={fileInputId}>Template zip</FieldLabel>
            <Input
              id={fileInputId}
              type="file"
              accept=".zip"
              disabled={busy}
              onChange={(e) => onFile(e.target.files?.[0])}
            />
          </Field>
          {busy && <Tiny>Staging…</Tiny>}
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </>
      )}

      {step === "manifest" && staged && (
        <form
          id={formId}
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void doCommit();
          }}
        >
          {staged.warnings.length > 0 && (
            <div className="space-y-1">
              {staged.warnings.map((w, i) => (
                <WarnText key={i} className="flex items-start gap-1.5">
                  <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
                  <span>{w}</span>
                </WarnText>
              ))}
            </div>
          )}
          {hasRecs && validCount > 0 && (
            <div className="space-y-1">
              <Label className="cursor-pointer">
                <Checkbox checked={applyRecs} onCheckedChange={(c) => setApplyRecs(c === true)} />
                <span>Apply author's recommended bindings</span>
              </Label>
              <Tiny className="block pl-6">
                {applyRecs
                  ? "Author of this bundle suggests file extensions for each template. Toggle chips to accept or reject."
                  : "Bindings skipped — templates import as unbound. Bind later in File bindings tab."}
              </Tiny>
            </div>
          )}
          {validCount === 0 ? (
            <Muted>
              No valid template folders found in this zip (each needs a{" "}
              <Identifier>template.html</Identifier>).
            </Muted>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-card">
              {staged.items.map((it) => (
                <Fragment key={it.name}>
                  <ImportRow
                    item={it}
                    resolution={resolutions[it.name] ?? "skip"}
                    onResolution={(r) => setRes(it.name, r)}
                  />
                  {it.valid && (chips[it.name]?.length ?? 0) > 0 && (
                    <ChipStrip
                      chips={chips[it.name]}
                      enabled={applyRecs}
                      skipped={isSkipped(it)}
                      keepBoth={isKeepBoth(it)}
                      onToggle={(key) => toggleChip(it.name, key)}
                      onAdd={(key) => addChip(it.name, key)}
                    />
                  )}
                </Fragment>
              ))}
            </div>
          )}
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </form>
      )}

      {step === "done" && result && (
        <div className="space-y-1">
          <ResultLine label="Imported" names={result.imported} />
          <ResultLine
            label="Renamed"
            names={Object.entries(result.renamed).map(([from, to]) => from + " → " + to)}
          />
          <ResultLine label="Overwritten" names={result.overwritten} />
          <ResultLine label="Skipped" names={result.skipped} />
          {(result.bindingsApplied?.length ?? 0) > 0 && (
            <>
              <div>
                <Tiny>Bindings applied:</Tiny> {result.bindingsApplied!.length}
              </div>
              <Identifier className="block whitespace-normal">{groupAppliedBindings(result.bindingsApplied!)}</Identifier>
            </>
          )}
        </div>
      )}
    </TemplatesDialog>
  );
}

// Tabs-like stepper: the current step is the filled pill, completed steps
// show a check, upcoming steps are muted. Display-only — the wizard advances
// by staging / committing, never by clicking a step.
function Stepper({ current }: { current: WizardStep }) {
  const idx = STEPS.findIndex((s) => s.id === current);
  return (
    <ol className="flex items-center gap-1" aria-label="Import steps">
      {STEPS.map((s, i) => {
        const state = i < idx ? "done" : i === idx ? "current" : "todo";
        return (
          <li key={s.id} className="flex items-center gap-1">
            {i > 0 && <span className="h-px w-4 bg-border" aria-hidden />}
            <Button
              type="button"
              size="xs"
              variant={state === "current" ? "default" : "ghost"}
              disabled={state !== "current"}
              aria-current={state === "current" ? "step" : undefined}
              className={cn(state === "todo" && "text-muted-foreground", state === "done" && "disabled:opacity-100")}
              tabIndex={-1}
            >
              {state === "done" ? <CheckIcon data-icon="inline-start" /> : <span className="tabular-nums">{i + 1}</span>}
              {s.label}
            </Button>
          </li>
        );
      })}
    </ol>
  );
}

// Compact grouped summary of applied bindings, e.g.
// ".geojson, .parquet → geo-heatmap · .csv, .tsv → csv-2".
function groupAppliedBindings(applied: { key: string; template: string }[]): string {
  const byTemplate = new Map<string, string[]>();
  for (const b of applied) {
    const keys = byTemplate.get(b.template);
    if (keys) keys.push(b.key);
    else byTemplate.set(b.template, [b.key]);
  }
  return Array.from(byTemplate.entries())
    .map(([template, keys]) => keys.join(", ") + " → " + template)
    .join(" · ");
}

function ResultLine({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div>
      <Tiny>{label}:</Tiny> {names.join(", ")}
    </div>
  );
}

// A registry key is a dot extension (".csv"), a directory pattern ("dir/") or
// the root directory key ("/") — the same shapes the bindings tab accepts.
function isValidCustomKey(key: string): boolean {
  return key.length > 0 && (key.startsWith(".") || key.endsWith("/"));
}

// Strip of recommended-binding chips under a manifest row. Inert (greyed,
// non-interactive) when the master toggle is off or the template resolves to
// skip — nothing is sent for those.
function ChipStrip({
  chips,
  enabled,
  skipped,
  keepBoth,
  onToggle,
  onAdd,
}: {
  chips: BindingChip[];
  enabled: boolean;
  skipped: boolean;
  keepBoth: boolean;
  onToggle: (key: string) => void;
  onAdd: (key: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftErr, setDraftErr] = useState(false);
  const inert = !enabled || skipped;

  const commitDraft = () => {
    const key = draft.trim();
    if (!isValidCustomKey(key)) {
      setDraftErr(true);
      return;
    }
    onAdd(key);
    setDraft("");
    setDraftErr(false);
    setAdding(false);
  };

  const reEnabled = chips.filter((c) => c.status === "disabled" && c.on);
  const anyOn = chips.some((c) => c.on && (keepBoth || c.status !== "already-bound"));

  const chipCls =
    "inline-flex h-6 items-center gap-1 rounded-md border px-1.5 font-mono text-xs transition-colors disabled:pointer-events-none";

  return (
    <div className={cn("border-b border-border bg-muted/30 px-4 py-2 last:border-b-0", inert && "opacity-50")}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Tiny className="mr-1">Recommended for:</Tiny>
        {chips.map((c) =>
          // "already bound" only holds for the ORIGINAL name — under
          // keep-both the copy lands renamed and unbound, so these chips
          // become normal toggles (default ON, badge dropped).
          c.status === "already-bound" && !keepBoth ? (
            <span key={c.key} className={cn(chipCls, "border-dashed border-border text-muted-foreground")}>
              {c.key}
              <Tiny className="font-sans">already bound</Tiny>
            </span>
          ) : (
            <button
              key={c.key}
              type="button"
              aria-pressed={c.on}
              className={cn(
                chipCls,
                c.on
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-transparent text-muted-foreground line-through hover:text-foreground",
              )}
              onClick={() => onToggle(c.key)}
              disabled={inert}
              title={c.on ? "Click to skip binding " + c.key : "Click to bind " + c.key}
            >
              {c.on && <CheckIcon className="size-3" />}
              {c.key}
              {c.status === "disabled" && (
                <span className="font-sans text-[10px] opacity-80">disabled by you</span>
              )}
            </button>
          ),
        )}
        {adding ? (
          <Input
            type="text"
            className="h-6 w-28 font-mono text-xs"
            value={draft}
            placeholder=".ext or dir/"
            autoFocus
            aria-invalid={draftErr || undefined}
            onChange={(e) => {
              setDraft(e.target.value);
              setDraftErr(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitDraft();
              }
              if (e.key === "Escape") {
                // Cancel the draft without letting the dialog's Escape handler
                // close the whole wizard.
                e.stopPropagation();
                setDraft("");
                setDraftErr(false);
                setAdding(false);
              }
            }}
            onBlur={() => {
              if (draft.trim() === "") {
                setAdding(false);
                setDraftErr(false);
              }
            }}
          />
        ) : (
          <button
            type="button"
            className={cn(chipCls, "border-dashed border-border text-muted-foreground hover:border-solid hover:text-foreground")}
            onClick={() => setAdding(true)}
            disabled={inert}
            title="Bind an extra extension to this template"
          >
            + add
          </button>
        )}
      </div>
      {!inert &&
        reEnabled.map((c) => (
          <WarnText key={c.key} className="mt-1">
            Checking {c.key} re-enables an extension you disabled.
          </WarnText>
        ))}
      {!inert && keepBoth && anyOn && (
        <WarnText className="mt-1">
          Will bind under the renamed copy — added after your existing templates on these extensions.
        </WarnText>
      )}
    </div>
  );
}

function ImportRow({
  item,
  resolution,
  onResolution,
}: {
  item: ImportItem;
  resolution: ImportResolution;
  onResolution: (r: ImportResolution) => void;
}) {
  if (!item.valid) {
    return (
      <div className="flex items-center gap-3 border-b border-border px-4 py-2 text-sm text-muted-foreground last:border-b-0">
        <span className="font-medium line-through">{item.name}</span>
        <Tiny>
          skipped — no <Identifier>template.html</Identifier>
        </Tiny>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-2 text-sm last:border-b-0">
      <span className="font-medium">{item.name}</span>
      <Tiny>{item.fileCount} files</Tiny>
      <span className="ml-auto flex items-center gap-3">
        {item.conflictsExisting ? (
          <>
            <Badge variant="outline">conflicts existing</Badge>
            <FilterGroup<ImportResolution>
              ariaLabel={"Resolution for " + item.name}
              value={resolution}
              onChange={onResolution}
              options={[
                { value: "overwrite", label: "Overwrite", title: "Replace the existing folder (destructive)", className: "data-pressed:bg-destructive/15 data-pressed:text-destructive aria-pressed:bg-destructive/15 aria-pressed:text-destructive" },
                { value: "skip", label: "Skip", title: "Keep the existing folder, drop this one" },
                { value: "keep-both", label: "Keep both", title: "Land as a new -2 folder" },
              ]}
            />
          </>
        ) : (
          <>
            <Tiny>new</Tiny>
            <Tiny>will import</Tiny>
          </>
        )}
      </span>
    </div>
  );
}
