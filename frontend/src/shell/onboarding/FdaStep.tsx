// Step 3 — Full Disk Access (macOS).
//
// This deliberately REVERSES FdaStrip's "not at launch" rule (FdaStrip.tsx):
// the strip waits for a refused read because an unprompted whole-disk ask is
// the wrong first impression on a page. Inside a setup wizard the user has
// opted into being asked — and the silent-deny failure the strip documents
// (a backend read under Documents while the app is not frontmost gets its
// prompt suppressed and a DENY cached) is exactly what a grant here prevents.
// Both coexist: the wizard is the front door, the strip is the floor.
//
// macOS has no API to request FDA. Explain + open the exact Settings pane is
// the whole affordance. The button is live only when the server offers it
// (`config.fda` present: packaged mac app, conclusive probe); on a dev server
// the copy still renders and the button says why it is disabled.
import { useEffect, useState } from "react";
import { Check, ExternalLink, FolderLock, Lock, ShieldCheck } from "lucide-react";

import { getConfig, openFdaSettings, type Config } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";

import { StepHeader } from "./StepHeader";

//: Poll for the grant while the pane is open. Most grants apply to a relaunched
//: process only, but when macOS relaunches the app for us the fresh server
//: answers granted and this still-open tab converges.
const GRANT_POLL_MS = 3000;

export function FdaStep({ config, eyebrow }: { config: Config; eyebrow: string }) {
  const [fda, setFda] = useState(config.fda);
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const offered = fda !== undefined;
  const granted = fda?.granted === true;

  useEffect(() => {
    if (!opened || granted) return;
    let alive = true;
    const t = window.setInterval(() => {
      getConfig().then(
        (c) => {
          if (alive) setFda(c.fda);
        },
        () => undefined,
      );
    }, GRANT_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [opened, granted]);

  const open = () => {
    setError(null);
    openFdaSettings().then(
      () => setOpened(true),
      (e) => setError(String(e?.message || e)),
    );
  };

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Let FusedRender read your files"
        lead="macOS asks separately for Desktop, Documents, Downloads, external drives and network volumes — and if a prompt fires while the app is in the background, it is silently denied. Full Disk Access, granted once in System Settings, covers all of them and survives upgrades."
      />

      <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-3">
        {[
          {
            icon: <Lock className="size-4" />,
            title: "Nothing leaves this machine",
            body: "No account, no cloud, no telemetry. Your files stay on disk; only your own Claude Code session ever reads them.",
          },
          {
            icon: <FolderLock className="size-4" />,
            title: "One grant, every folder",
            body: "Replaces a prompt per protected folder — including the ones macOS never shows.",
          },
          {
            icon: <ShieldCheck className="size-4" />,
            title: "You stay in control",
            body: "Revoke it any time in System Settings › Privacy & Security.",
          },
        ].map((c) => (
          <li key={c.title} className="flex flex-col gap-1.5 rounded-xl border border-border bg-card p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="grid size-7 place-items-center rounded-md bg-muted text-foreground">{c.icon}</span>
              {c.title}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{c.body}</p>
          </li>
        ))}
      </ul>

      <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
        {granted ? (
          <div className="flex items-center gap-3">
            <span className="grid size-6 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
              <Check className="size-3.5" strokeWidth={3} />
            </span>
            <div>
              <div className="text-sm font-medium text-muted-foreground line-through decoration-emerald-500/60">
                Grant Full Disk Access
              </div>
              <div className="text-xs text-muted-foreground">Already granted — nothing to do here.</div>
            </div>
          </div>
        ) : (
          <>
            <ol className="m-0 flex list-none flex-col gap-1.5 p-0 text-sm">
              <li className="flex gap-2">
                <span className="w-4 shrink-0 text-muted-foreground">1.</span>
                <span>Open System Settings on the Full Disk Access pane.</span>
              </li>
              <li className="flex gap-2">
                <span className="w-4 shrink-0 text-muted-foreground">2.</span>
                <span>Turn on <strong className="font-medium">FusedRender</strong> in the list.</span>
              </li>
              <li className="flex gap-2">
                <span className="w-4 shrink-0 text-muted-foreground">3.</span>
                <span>Relaunch the app when macOS asks — the grant applies to the next launch.</span>
              </li>
            </ol>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={open} disabled={!offered} title={offered ? undefined : "Available in the installed FusedRender app"}>
                <ExternalLink data-icon="inline-start" />
                {opened ? "Open System Settings again" : "Open System Settings"}
              </Button>
              {!offered && (
                <span className="text-xs text-muted-foreground">
                  Available in the installed FusedRender app — a dev server's grant would land on the terminal that launched it.
                </span>
              )}
              {opened && offered && (
                <span className="text-xs text-muted-foreground" role="status">
                  Waiting for the grant… turn FusedRender on in the pane that just opened.
                </span>
              )}
            </div>
            {error && <p className="text-xs text-destructive">{error}</p>}
          </>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        You can do this later. If macOS ever refuses a file, the Home page shows a reminder with this same button.
      </p>
    </div>
  );
}
