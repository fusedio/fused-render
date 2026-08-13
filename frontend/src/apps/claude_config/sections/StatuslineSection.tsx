// Statusline section: read-only introspection of settings.json → statusLine,
// plus a live preview.
//
// The preview runs the configured command against a synthetic sample payload
// (the one place the app executes a user command — guarded server-side: sh -c,
// 5s timeout, output capped, nothing mutated). Its stdout arrives with ANSI
// escapes intact and is rendered through ./ansi.tsx so the preview reads the way
// the terminal does. It runs once on open, and again on demand.
import { useCallback, useEffect, useState } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import { renderAnsi } from "../ansi";
import { Card, CardSub, CardTitle, Empty, SKELETON_ROWS, useModuleData } from "../bits";

export default function StatuslineSection() {
  const load = useCallback(() => cc.statusline.get(), []);
  const { data, error } = useModuleData(load);
  const [running, setRunning] = useState(false);
  const [output, setOutput] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const configured = !!data?.configured;

  const preview = useCallback(async () => {
    setRunning(true);
    setPreviewError(null);
    try {
      const res = await cc.statusline.preview();
      if (res.ok) setOutput(res.output);
      else setPreviewError(res.error || "preview failed");
    } catch (e) {
      setPreviewError((e as Error).message);
    } finally {
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
  if (!data.configured) return <Empty>No status line configured.</Empty>;

  const sc = data.script;
  return (
    <Card>
      <CardTitle>Status line</CardTitle>
      <CardSub mono>{data.command}</CardSub>
      {sc ? (
        <>
          {sc.description && <CardSub>{sc.description}</CardSub>}
          <CardSub>
            {sc.tracked ? "tracked ✓" : "not tracked"} · {sc.size} bytes · modified{" "}
            {new Date(sc.modified).toLocaleString()}
          </CardSub>
          {sc.fields.length ? (
            <CardSub>
              <strong>Shows:</strong> {sc.fields.join(" · ")}
            </CardSub>
          ) : (
            <CardSub>Couldn&apos;t introspect this command&apos;s fields.</CardSub>
          )}
          {sc.otherFields.length > 0 && <CardSub>Also reads: {sc.otherFields.join(", ")}</CardSub>}
        </>
      ) : (
        <CardSub>
          This command doesn&apos;t point at a local script we can read — showing the command only.
        </CardSub>
      )}
      <div className="cc-card-actions">
        <button type="button" className="btn" disabled={running} onClick={() => void preview()}>
          Re-run preview
        </button>
      </div>
      <pre className="cc-pre cc-mono">
        {running && output === null
          ? "Running…"
          : previewError
            ? `Preview failed: ${previewError}`
            : output
              ? renderAnsi(output)
              : "(empty output)"}
      </pre>
      {sc && (
        <CardSub>
          Script: <span className="cc-mono">{sc.path}</span>
        </CardSub>
      )}
    </Card>
  );
}
