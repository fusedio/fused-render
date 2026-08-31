// Full Disk Access warning — a strip on the front door, in the same posture
// and styling as ClaudeHealthStrip.
//
// It replaced the touch-triggered notification card (FdaCard). The card only
// appeared once the session read under a protected folder, on the theory that
// the per-folder Allow prompts would cover everyone else. On a fresh machine
// that theory fails in the worst way: a backend read under Desktop/Documents
// while the app is not frontmost gets its prompt suppressed and a DENY cached
// silently, so the user's first sign of trouble is "permission denied" with
// no prompt ever shown and no idea why. Full Disk Access — granted once in
// System Settings — sidesteps the whole category.
//
// WHEN it shows is the design: not at launch (an up-front whole-disk ask on
// a machine whose files all open fine is the wrong first impression), but the
// moment an fs route actually hits a PermissionError — the server flips
// `fda.denied` on that first denial (fused_render/shell/fda.py), which also
// catches the silent-deny case above, since the cached deny still surfaces
// as EPERM on the next read. Until then this renders nothing and quietly
// watches /api/config.
//
// macOS has no API to request FDA; explain + open the exact Settings pane is
// the entire affordance. The `fda` field of /api/config gates everything
// (fused_render/shell/fda.py): absent (non-mac, dev server, inconclusive
// probe) means render nothing. "✕" acknowledges the CURRENT denial on the
// server — every tab converges to hidden — and the next PermissionError
// raises strip and toast again: dismiss is "not now", not "never".
import { useEffect, useRef, useState } from "react";

import { dismissFdaNudge, getConfig, openFdaSettings } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";

//: While showing, watch for the grant landing. Most grants only apply to a
//: relaunched process, but when macOS relaunches the app for us the fresh
//: server answers granted and a still-open tab converges — and another tab's
//: dismissal converges the same way.
const GRANT_POLL_MS = 3000;

//: While hidden but eligible (fda present, not granted), watch for the next
//: denial so the strip appears when trouble starts — or starts again.
const WATCH_MS = 5000;

// hidden → (watching) → showing. "watching" renders nothing: the machine
// qualifies but no fs route has been refused yet.
type Stage = "hidden" | "watching" | "showing";

//: One toast per DENIAL EPISODE, not per tab lifetime: the strip re-detects
//: `denied` on every remount, so the flag suppresses repeats — but a
//: dismissal ends the episode (noteNotDenied resets this), and the next
//: denial gets its own toast. The toast just breaks the news through the
//: app's normal queue; the strip itself is the explanation and the fix.
let toastShown = false;

function noteDenialToast() {
  if (toastShown) return;
  toastShown = true;
  pushToast({
    msg: "macOS denied FusedRender access to a file — grant Full Disk Access to fix this",
    tone: "error",
  });
}

function noteNotDenied() {
  toastShown = false;
}

// The last stage seen, so walking between Home and /apps — which both render
// this — starts from what we already know instead of flashing a visible strip
// away and back while the fresh getConfig is in flight (same seed-not-
// short-circuit pattern as ClaudeHealthStrip's `cached`). The fetch still
// runs on every mount and overwrites this.
let cachedStage: Stage = "hidden";

export function FdaStrip() {
  const [stage, setStageState] = useState<Stage>(cachedStage);
  const setStage = (next: Stage) => {
    cachedStage = next;
    setStageState(next);
  };
  // "Open System Settings" was pressed: the steps collapse into a one-line
  // "turn it on over there" so the strip reads as in-progress, not nagging.
  const [waiting, setWaiting] = useState(false);
  const stageRef = useRef(stage);
  stageRef.current = stage;
  const showing = stage === "showing";

  useEffect(() => {
    let disposed = false;
    getConfig()
      .then((config) => {
        if (disposed) return;
        const fda = config.fda;
        // Explicit "hidden" rather than an early return: the seeded stage can
        // say "showing" from a previous mount while a grant or another tab's
        // dismissal has since landed.
        if (!fda || fda.granted) setStage("hidden");
        else if (fda.denied) {
          noteDenialToast();
          setStage("showing");
        } else {
          noteNotDenied();
          setStage("watching");
        }
      })
      .catch(() => {}); // no config, no warning — the server card owns outages
    return () => {
      disposed = true;
    };
  }, []);

  // While "watching", wait for the next denial.
  useEffect(() => {
    if (stage !== "watching") return;
    const timer = window.setInterval(() => {
      getConfig()
        .then((config) => {
          if (stageRef.current !== "watching") return;
          const fda = config.fda;
          if (!fda || fda.granted) setStage("hidden");
          else if (fda.denied) {
            noteDenialToast();
            setStage("showing");
          }
        })
        .catch(() => {});
    }, WATCH_MS);
    return () => window.clearInterval(timer);
  }, [stage]);

  useEffect(() => {
    if (!showing) return;
    const timer = window.setInterval(() => {
      getConfig()
        .then((config) => {
          if (stageRef.current !== "showing") return;
          const fda = config.fda;
          if (fda?.granted) {
            setStage("hidden");
            // "info" is this app's confirmation tone — there is no green toast
            // (Toast.tsx), and every other success ("Path copied") is info too.
            pushToast({ msg: "Full Disk Access is on — no more prompts", tone: "info" });
          } else if (!fda) {
            setStage("hidden");
          } else if (!fda.denied) {
            // Another tab dismissed (the server cleared its flag). Back to
            // watching, not hidden: the next denial must resurface the strip —
            // and start it on the steps, not a stale "In System Settings…".
            noteNotDenied();
            setWaiting(false);
            setStage("watching");
          }
        })
        .catch(() => {});
    }, GRANT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [showing]);

  if (!showing) return null;

  // "Not now": acknowledge on the server (clears `denied`, every tab hides)
  // and go back to watching so the next denial brings strip + toast back.
  const close = () => {
    noteNotDenied();
    // Next episode starts on the steps, not a stale "In System Settings…".
    setWaiting(false);
    setStage("watching");
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
            macOS just refused FusedRender access to a file or folder —
            sometimes it does this with no permission prompt at all, just
            "permission denied". Granting Full Disk Access once fixes Desktop,
            Documents, Downloads and external volumes permanently. FusedRender
            is completely local: your files are read on this Mac and no data
            ever leaves your computer.
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
