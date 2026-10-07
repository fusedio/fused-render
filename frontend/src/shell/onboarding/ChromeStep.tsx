import { useEffect, useState, type ReactNode } from "react";
// Step 3 — Google Chrome. Bots drive a real Chrome of their own (fused_render/
// bots/browser.py launches it over CDP), so a machine without one cannot run
// a bot at all — and nothing before the first bot says so except the Bots
// page's empty hero. This step says it up front, in both flavors: Fused Bot
// is nothing but bots, and Render runs the same ones behind `bots_enabled`.
//
// NOTHING TO WRITE. Whether a Chrome is on disk is the server's to know
// (shell/onboarding.py `_observe` walks the bots' own candidate list on every
// read, the same walk a starting bot does), so this step only READS its
// stage: `complete` with `meta.path` = found; `pending` = not found where a
// bot could run; `n/a` = not found and no bot will run here (Render, bots
// off) — the meter leaves it out, but the step still explains. A found
// Chromium / Edge / Brave counts too: the candidate list is the bots' own.
//
// Installing Chrome happens outside this window, so the step re-reads when
// the window comes back (focus — visibilitychange alone does not fire when a
// native window merely regains focus) and on a "Check again" button. macOS
// has no API to install a browser: a download link is the whole affordance.
import { Check, Download, Eye, Globe, RefreshCw, UserRound, Wand2 } from "lucide-react";

import { isBot } from "@platform/lib/flavor";
import { Button } from "@platform/shadcn/ui/button";

import { refreshProgress, type Stages } from "./progress";
import { StepHeader } from "./StepHeader";

// An external http(s) link: the native window hands it to the default
// browser (mac_window.py), a browser tab opens a new tab.
const DOWNLOAD_URL = "https://www.google.com/chrome/";

// "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" → "Google
// Chrome", "/usr/bin/google-chrome" → "google-chrome": the app, not its binary.
function appName(path: string): string {
  const m = path.match(/([^/]+)\.app\//);
  return m ? m[1] : path.split("/").pop() || path;
}

export function ChromeStep({
  stages,
  eyebrow,
  onWork,
}: {
  stages: Stages | undefined;
  eyebrow: ReactNode;
  onWork: (busy: boolean) => void;
}) {
  const stage = stages?.chrome;
  const found = stage?.status === "complete";
  const path = typeof stage?.meta?.path === "string" ? (stage.meta.path as string) : null;
  // Where no bot will run (Render, bots off), the step is advisory: Next is
  // the yellow button straight away. Elsewhere Download owns it until found.
  const needed = isBot() || stage?.status === "pending";
  useEffect(() => onWork(needed && !found), [onWork, needed, found]);

  const [checking, setChecking] = useState(false);
  const check = () => {
    setChecking(true);
    refreshProgress().finally(() => setChecking(false));
  };
  // Fresh on arrival, and again whenever the window comes back from the
  // installer / the browser the download link opened.
  useEffect(() => {
    void refreshProgress();
    const onFocus = () => void refreshProgress();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <StepHeader
        eyebrow={eyebrow}
        title="Install Google Chrome"
        lead={
          isBot()
            ? "Every bot drives a real Chrome window of its own — the same browser you use, with its own profile, so it can sign in to sites, fill forms and read pages the way you would. Without Chrome, a bot has nothing to drive."
            : "Bots drive a real Chrome window of their own — the same browser you use, with its own profile. You only need it if you turn bots on; apps and local models work without it."
        }
      />

      <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-2 lg:grid-cols-4">
        {[
          {
            icon: <UserRound className="size-4" />,
            title: "Its own profile",
            body: "Each bot gets a separate Chrome profile — cookies, logins and history of its own. It never touches the Chrome you browse in.",
          },
          {
            icon: <Eye className="size-4" />,
            title: "Watch it work",
            body: "The bot's window streams live into its page. Take over to solve a captcha or sign in, then hand it back.",
          },
          {
            icon: <Wand2 className="size-4" />,
            title: "Start from your logins",
            body: "A bot can import a copy of one of your Chrome profiles — extensions and sessions included — so it starts signed in.",
          },
          {
            icon: <Globe className="size-4" />,
            title: "Chromium works too",
            body: "An installed Chromium, Microsoft Edge or Brave is found the same way. Google Chrome is the one we test with.",
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
        {found ? (
          <div className="flex items-center gap-3">
            <span className="grid size-6 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
              <Check className="size-3.5" strokeWidth={3} />
            </span>
            <div className="min-w-0">
              <div className="text-sm font-medium text-muted-foreground line-through">Install Google Chrome</div>
              <div className="truncate text-xs text-muted-foreground" title={path ?? undefined}>
                {path ? `Found ${appName(path)} — nothing to do here.` : "Already installed — nothing to do here."}
              </div>
            </div>
          </div>
        ) : (
          <>
            <ol className="m-0 flex list-none flex-col gap-1.5 p-0 text-sm">
              {[
                "Download Chrome from google.com/chrome and install it like any other app.",
                "Come back here — the check runs again on its own.",
              ].map((s, i) => (
                <li key={s} className="flex gap-2">
                  <span className="w-4 shrink-0 text-muted-foreground">{i + 1}.</span>
                  <span>{s}</span>
                </li>
              ))}
            </ol>
            <div className="flex flex-wrap items-center gap-3">
              <Button variant={needed ? "accent" : "outline"} render={<a href={DOWNLOAD_URL} target="_blank" rel="noreferrer" />}>
                <Download data-icon="inline-start" />
                Download Chrome
              </Button>
              <Button variant="ghost" size="sm" onClick={check} disabled={checking}>
                <RefreshCw data-icon="inline-start" className={checking ? "animate-spin" : undefined} />
                Check again
              </Button>
              <span className="text-xs text-muted-foreground" role="status">
                {stage ? "No Chrome found on this machine." : "Checking…"}
              </span>
            </div>
          </>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        You can do this later. The Bots page says so again until a Chrome is found, and a bot that starts without one stops with the same message.
      </p>
    </div>
  );
}
