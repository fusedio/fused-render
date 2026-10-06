// The empty hero's machine check (replaces FusedBot's link into its own setup wizard): GET /api/bots/setup once,
// then one line per missing piece — no Chrome to drive, no Claude Code to think with — and a link to fused-render's
// own first-run wizard. Nothing shows while the check is in flight or when it fails (the hero must never block on it).
import { useEffect, useState } from "react";
import { api, type SetupReply } from "../lib/api";
import { setupIssues } from "../lib/setup";

// fused-render's wizard route (shell/onboarding/state.ts ONBOARDING_PATH). A literal: apps may not import shell.
const SETUP_PATH = "/onboarding";

/** `first`: no bots yet, so the setup link shows even when nothing is missing (FusedBot showed it then too). */
export function SetupLines({ first }: { first: boolean }) {
  const [s, setS] = useState<SetupReply | null>(null);
  useEffect(() => {
    let live = true;
    api.setup().then((r) => { if (live) setS(r); }, () => { /* no check, no lines */ });
    return () => { live = false; };
  }, []);
  const issues = setupIssues(s);
  if (!first && !issues.length) return null;
  return (
    <>
      {issues.map((t) => <div key={t} className="setupissue">{t}</div>)}
      <a className="muted setuplink" href={SETUP_PATH}>Set up this Mac (Claude Code, Chrome, local models)</a>
    </>
  );
}
