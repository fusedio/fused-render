// Statusline section: read-only introspection of settings.json → statusLine,
// plus a live preview.
//
// The preview runs the configured command against a synthetic sample payload
// (the one place the app executes a user command — guarded server-side: sh -c,
// 5s timeout, output capped, nothing mutated). Its stdout arrives with ANSI
// escapes intact and is rendered through ./ansi.tsx so the preview reads the way
// the terminal does. It runs once on open, and again on demand.
//
// The preview IS the content, so it is the first thing on the page — in the
// log-viewer shape (a dark mono block). The facts about the file follow as a
// property list UNDER the preview, where metadata belongs, and re-running it is
// the same toolbar refresh icon every other tab has.
import { useCallback, useEffect, useRef, useState } from "react";
import { PropertyList, PropertyRow } from "@platform/ui/flow/PropertyRow";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import * as cc from "../api";
import { LOG_ERROR_CLASS, renderAnsi } from "../ansi";
import { Code, Empty, ErrorNote, ListSkeleton, SKELETON_ROWS, SectionToolbar, useModuleData } from "../bits";

export default function StatuslineSection() {
  const load = useCallback(() => cc.statusline.get(), []);
  const { data, error } = useModuleData(load);
  const [running, setRunning] = useState(false);
  // A ref, not the state flag: two clicks in the same tick both read the same
  // stale `running === false` before React re-renders.
  const runningRef = useRef(false);
  const [output, setOutput] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const configured = !!data?.configured;

  const preview = useCallback(async () => {
    // The toolbar disables its refresh while this runs (refreshBusy below), but
    // the guard lives here too: this is the one place the app executes the
    // user's own command, and "can't be clicked again" must not depend on the
    // control that happens to call it.
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    setPreviewError(null);
    try {
      const res = await cc.statusline.preview();
      if (res.ok) setOutput(res.output);
      else setPreviewError(res.error || "preview failed");
    } catch (e) {
      setPreviewError((e as Error).message);
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  }, []);

  // Auto-run once the command is known to exist — a statusline you can't see
  // rendered tells you nothing, so the preview is the section's content, not an
  // optional extra.
  useEffect(() => {
    if (configured) void preview();
  }, [configured, preview]);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading status line" />;
  if (!data.configured) {
    // No action offered, deliberately: this tab is a viewer, and configuring a
    // status line means putting a command in settings.json (or letting the
    // `claude` CLI do it). Saying that plainly beats inventing a control.
    return (
      <Empty>
        No status line configured. Claude Code shows its default until settings.json has a{" "}
        <Code>statusLine</Code> command.
      </Empty>
    );
  }

  const sc = data.script;
  return (
    <>
      <SectionToolbar
        summary={
          sc
            ? `${sc.fields.length} field(s) detected · ${sc.tracked ? "tracked" : "not tracked"}`
            : "command only — no local script to read"
        }
        onRefresh={() => void preview()}
        refreshLabel="Re-run preview"
        refreshBusy={running}
      />
      {/* The hero: what the thing actually looks like. Log-viewer shape — the
          block is always dark, so its base text is fixed light rather than the
          theme's foreground (which is near-black in the light theme). */}
      {/* Bordered as well as tinted: in the dark theme neutral-950 is within a
          hair of the page background, so the fill alone left the block with no
          edge at all. The border is what makes it read as a block there; in the
          light theme the fill was already doing that on its own. */}
      <div className="relative border border-border bg-neutral-950 rounded-lg p-3 font-mono text-xs text-neutral-200">
        {running && (
          <StatusDot bucket="yellow" pulse label="Running" className="absolute top-3 right-3" />
        )}
        <pre className="m-0 whitespace-pre-wrap break-words font-mono text-xs">
          {running && output === null
            ? "Running…"
            : previewError
              ? <span className={LOG_ERROR_CLASS}>Preview failed: {previewError}</span>
              : output
                ? renderAnsi(output)
                : "(empty output)"}
        </pre>
      </div>
      {/* The composite's `dd` hugs its content on the right, which is the look
          a narrow properties panel wants. Here the property list IS the page
          and the values are paths and sentences, so `flex-1` on the value cell
          gives them a left-aligned column of their own — without it
          `text-left` has nothing to align inside. */}
      <PropertyList className="max-w-3xl [&_dd]:flex-1 [&_dd]:text-left [&_dd]:whitespace-normal [&_dt]:w-24">
        <PropertyRow label="Command">
          <Code>{data.command}</Code>
        </PropertyRow>
        {sc ? (
          <>
            {sc.description && <PropertyRow label="Description">{sc.description}</PropertyRow>}
            <PropertyRow label="Script">
              <Code>{sc.path}</Code>
            </PropertyRow>
            <PropertyRow label="File">
              {sc.tracked ? "tracked in your config repo" : "not tracked"} · {sc.size} bytes · modified{" "}
              {new Date(sc.modified).toLocaleString()}
            </PropertyRow>
            <PropertyRow label="Shows">
              {sc.fields.length ? sc.fields.join(" · ") : "couldn't introspect this command's fields"}
            </PropertyRow>
            {sc.otherFields.length > 0 && (
              <PropertyRow label="Also reads">{sc.otherFields.join(", ")}</PropertyRow>
            )}
          </>
        ) : (
          <PropertyRow label="Script">
            This command doesn&apos;t point at a local script we can read — showing the command only.
          </PropertyRow>
        )}
      </PropertyList>
    </>
  );
}
