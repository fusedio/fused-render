// Full Disk Access warning — a strip on the front door, in the same posture
// as ClaudeHealthStrip: says what is still needed before the user has spent
// anything finding out.
//
// It replaced the touch-triggered notification card (FdaCard). The card only
// appeared once the session read under a protected folder, on the theory that
// the per-folder Allow prompts would cover everyone else. On a fresh machine
// that theory fails in the worst way: a backend read under Desktop/Documents
// while the app is not frontmost gets its prompt suppressed and a DENY cached
// silently, so the user's first sign of trouble is "permission denied" with
// no prompt ever shown and no idea why. Full Disk Access — granted once in
// System Settings — sidesteps the whole category, so the app now insists on
// it up front: the strip shows at launch whenever FDA is missing.
//
// macOS has no API to request FDA; explain + open the exact Settings pane is
// the entire affordance. The `fda` field of /api/config gates everything
// (fused_render/shell/fda.py): absent (non-mac, dev server, inconclusive
// probe) means render nothing, `dismissed` is persisted server-side so "✕"
// holds across sessions on this machine.
import { useEffect, useRef, useState } from "react";

import { dismissFdaNudge, getConfig, openFdaSettings } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";

//: While showing, watch for the grant landing. Most grants only apply to a
//: relaunched process, but when macOS relaunches the app for us the fresh
//: server answers granted and a still-open tab converges — and another tab's
//: dismissal converges the same way.
const GRANT_POLL_MS = 3000;

export function FdaStrip() {
  const [showing, setShowing] = useState(false);
  // "Open System Settings" was pressed: the steps collapse into a one-line
  // "turn it on over there" so the strip reads as in-progress, not nagging.
  const [waiting, setWaiting] = useState(false);
  const showingRef = useRef(showing);
  showingRef.current = showing;

  useEffect(() => {
    let disposed = false;
    getConfig()
      .then((config) => {
        if (disposed) return;
        const fda = config.fda;
        setShowing(Boolean(fda && !fda.granted && !fda.dismissed));
      })
      .catch(() => {}); // no config, no warning — the server card owns outages
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    if (!showing) return;
    const timer = window.setInterval(() => {
      getConfig()
        .then((config) => {
          if (!showingRef.current) return;
          const fda = config.fda;
          if (fda?.granted) {
            setShowing(false);
            // "info" is this app's confirmation tone — there is no green toast
            // (Toast.tsx), and every other success ("Path copied") is info too.
            pushToast({ msg: "Full Disk Access is on — no more prompts", tone: "info" });
          } else if (!fda || fda.dismissed) {
            setShowing(false);
          }
        })
        .catch(() => {});
    }, GRANT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [showing]);

  if (!showing) return null;

  const close = () => {
    setShowing(false);
    dismissFdaNudge().catch(() => {});
  };

  return (
    <section className="claude-health" role="status" aria-label="Full Disk Access setup">
      <div className="claude-health-head">
        {/* Says what is still needed, not that something is broken — same
            posture as ClaudeHealthStrip (SPEC §42: "Nothing red"). */}
        <h2 className="claude-health-title">Give FusedRender Full Disk Access</h2>
        <div className="claude-health-head-actions">
          <button
            type="button"
            className="claude-health-close"
            onClick={close}
            aria-label="Dismiss"
            title="Dismiss"
          >
            ✕
          </button>
        </div>
      </div>
      <ul className="claude-health-issues">
        <li className="claude-health-issue">
          <p className="claude-health-issue-detail">
            Without it, macOS blocks files in Desktop, Documents, Downloads and
            external volumes — sometimes with no permission prompt at all, just
            "permission denied". Granting Full Disk Access once fixes every
            folder permanently.
          </p>
          {waiting ? (
            <p className="claude-health-issue-detail">
              In System Settings: turn on FusedRender under Full Disk Access,
              then relaunch when asked.
            </p>
          ) : (
            <ol className="claude-health-issue-detail fda-steps">
              <li>Open System Settings</li>
              <li>Privacy &amp; Security → Full Disk Access</li>
              <li>Turn on FusedRender, relaunch when asked</li>
            </ol>
          )}
          <div className="claude-health-actions">
            <button
              type="button"
              className="claude-health-action"
              onClick={() => {
                setWaiting(true);
                openFdaSettings().catch(() => {});
              }}
            >
              {waiting ? "Reopen System Settings" : "Open System Settings"}
            </button>
          </div>
        </li>
      </ul>
    </section>
  );
}

export default FdaStrip;
