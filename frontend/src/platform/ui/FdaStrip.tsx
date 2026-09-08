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
// as EPERM on the next read. Until then this renders nothing.
//
// STATE lives in platform/lib/fda.ts — one store, one poll, one set of words,
// shared with the onboarding FdaStep — so every copy of this strip (Home,
// /apps, the explorer's AccessDenied card) and the wizard agree. This file
// only decides what to render for a given snapshot: absent → nothing;
// granted → nothing (plus one toast if we were showing); pending_relaunch →
// the Relaunch button; denied → the steps. "✕" acknowledges the CURRENT
// denial on the server — every tab converges to hidden — and the next
// PermissionError raises strip and toast again: dismiss is "not now", not
// "never".
import { useEffect, useRef, useState } from "react";

import { dismissFdaNudge, openFdaSettings } from "@platform/lib/api";
import { FDA_COPY, RELAUNCH_HREF, pokeFda, useFda } from "@platform/lib/fda";
import { pushToast } from "@platform/lib/toast";

//: One toast per DENIAL EPISODE, not per tab lifetime: the strip re-detects
//: `denied` on every remount, so the flag suppresses repeats — but a
//: dismissal ends the episode (the flag resets), and the next denial gets its
//: own toast. The toast just breaks the news through the app's normal queue;
//: the strip itself is the explanation and the fix.
let toastShown = false;

export function FdaStrip() {
  const fda = useFda();
  // "Open System Settings" was pressed: the steps collapse into a one-line
  // "turn it on over there" so the strip reads as in-progress, not nagging.
  const [waiting, setWaiting] = useState(false);
  // Whether THIS mount has shown the strip, so the grant toast fires only for
  // a user who was looking at the warning when the grant landed.
  const wasShowing = useRef(false);

  const showing = !!fda && !fda.granted && fda.denied;

  useEffect(() => {
    if (showing) {
      wasShowing.current = true;
      if (!toastShown) {
        toastShown = true;
        pushToast({ msg: FDA_COPY.deniedToast, tone: "error" });
      }
      return;
    }
    if (fda && !fda.denied) {
      // Dismissed here or in another tab: the episode is over, the next
      // denial gets its own toast and starts on the steps again.
      toastShown = false;
      setWaiting(false);
    }
    if (fda?.granted && wasShowing.current) {
      wasShowing.current = false;
      // "info" is this app's confirmation tone — there is no green toast
      // (Toast.tsx), and every other success ("Path copied") is info too.
      pushToast({ msg: FDA_COPY.grantedToast, tone: "info" });
    }
  }, [showing, fda]);

  if (!showing) return null;

  // "Not now": acknowledge on the server (clears `denied`, every tab hides)
  // and keep watching so the next denial brings strip + toast back.
  const close = () => {
    setWaiting(false);
    dismissFdaNudge().catch(() => {}).finally(pokeFda);
  };

  const pending = fda.pending_relaunch;

  return (
    <section className="claude-health" role="status" aria-label="Full Disk Access setup">
      <div className="claude-health-head">
        {/* Says what is still needed, not that something is broken — same
            posture as ClaudeHealthStrip (SPEC §42: "Nothing red"). */}
        <h2 className="claude-health-title">
          {pending ? "Relaunch to finish Full Disk Access" : "Give FusedRender Full Disk Access"}
        </h2>
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
          {pending ? (
            <>
              <p className="claude-health-issue-detail">{FDA_COPY.pending}</p>
              <div className="claude-health-actions">
                <a className="claude-health-action" href={RELAUNCH_HREF}>
                  {FDA_COPY.relaunch}
                </a>
              </div>
            </>
          ) : (
            <>
              <p className="claude-health-issue-detail">
                macOS just refused FusedRender access to a file or folder —
                sometimes it does this with no permission prompt at all, just
                "permission denied". Granting Full Disk Access once fixes Desktop,
                Documents, Downloads and external volumes permanently. FusedRender
                is completely local: your files are read on this Mac and no data
                ever leaves your computer.
              </p>
              {waiting ? (
                <p className="claude-health-issue-detail">{FDA_COPY.waiting}</p>
              ) : (
                <ol className="claude-health-issue-detail fda-steps">
                  {FDA_COPY.steps.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ol>
              )}
              <div className="claude-health-actions">
                <button
                  type="button"
                  className="claude-health-action"
                  onClick={() => {
                    setWaiting(true);
                    openFdaSettings().catch(() => {}).finally(pokeFda);
                  }}
                >
                  {waiting ? FDA_COPY.reopen : FDA_COPY.open}
                </button>
              </div>
            </>
          )}
        </li>
      </ul>
    </section>
  );
}

export default FdaStrip;
