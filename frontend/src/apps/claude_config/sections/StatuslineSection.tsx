// Statusline section: read-only introspection of settings.json → statusLine,
// plus a live preview.
//
// The preview runs the configured command against a synthetic sample payload
// (the one place the app executes a user command — guarded server-side: sh -c,
// 5s timeout, output capped, nothing mutated). Its stdout arrives with ANSI
// escapes intact and is rendered through ./ansi.tsx so the preview reads the way
// the terminal does. It runs once on open, and again on demand.
//
// The preview IS the content, so it is the first thing on the page. It used to
// come last, under a stack of CardSubs about tracked-ness and byte counts and
// field lists, which made the tab read as a debug dump of a file rather than as
// "here is your status line". Those facts are all still here — as a definition
// list UNDER the preview, where metadata belongs — and re-running it is the
// same toolbar refresh icon every other tab has rather than a bespoke button.
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import { renderAnsi } from "../ansi";
import { Empty, SKELETON_ROWS, SectionToolbar, useModuleData } from "../bits";

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

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={SKELETON_ROWS} label="Loading status line" />;
  if (!data.configured) {
    // No action offered, deliberately: this tab is a viewer, and configuring a
    // status line means putting a command in settings.json (or letting the
    // `claude` CLI do it). Saying that plainly beats inventing a control.
    return (
      <Empty>
        No status line configured. Claude Code shows its default until settings.json has a{" "}
        <span className="cc-mono">statusLine</span> command.
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
      {/* The hero: what the thing actually looks like. */}
      <pre className="cc-pre cc-mono cc-statusline">
        {running && output === null
          ? "Running…"
          : previewError
            ? `Preview failed: ${previewError}`
            : output
              ? renderAnsi(output)
              : "(empty output)"}
      </pre>
      <dl className="cc-dl">
        <dt className="cc-dl-key">Command</dt>
        <dd className="cc-dl-val cc-mono">{data.command}</dd>
        {sc ? (
          <>
            {sc.description && (
              <>
                <dt className="cc-dl-key">Description</dt>
                <dd className="cc-dl-val">{sc.description}</dd>
              </>
            )}
            <dt className="cc-dl-key">Script</dt>
            <dd className="cc-dl-val cc-mono">{sc.path}</dd>
            <dt className="cc-dl-key">File</dt>
            <dd className="cc-dl-val">
              {sc.tracked ? "tracked in your config repo" : "not tracked"} · {sc.size} bytes ·
              modified {new Date(sc.modified).toLocaleString()}
            </dd>
            <dt className="cc-dl-key">Shows</dt>
            <dd className="cc-dl-val">
              {sc.fields.length
                ? sc.fields.join(" · ")
                : "couldn't introspect this command's fields"}
            </dd>
            {sc.otherFields.length > 0 && (
              <>
                <dt className="cc-dl-key">Also reads</dt>
                <dd className="cc-dl-val">{sc.otherFields.join(", ")}</dd>
              </>
            )}
          </>
        ) : (
          <>
            <dt className="cc-dl-key">Script</dt>
            <dd className="cc-dl-val">
              This command doesn&apos;t point at a local script we can read — showing the command
              only.
            </dd>
          </>
        )}
      </dl>
    </>
  );
}
