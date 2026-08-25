// Full Disk Access nudge — a persistent card in the notification stack
// (NotificationHost owns placement, above the server-status card).
//
// macOS asks about each protected-folder category separately (Desktop,
// Documents, Downloads, removable, network volumes), so browsing them the
// first time is an "Allow… Allow… Allow" gauntlet. Full Disk Access, granted
// ONCE in System Settings, silences all of them permanently — and survives
// upgrades, because releases are Developer ID signed under a stable bundle id
// (D73). macOS has no API to request FDA, so this card is the whole
// affordance: explain, open the exact Settings pane, and get out of the way.
//
// WHEN it shows is the design: not at launch, but the moment this session
// first reads under a protected folder — the same moment macOS starts
// prompting, which is the only moment "tired of permission prompts?" is a
// question the user is actually asking themselves. The server flips
// `fda.relevant` on that first read (fused_render/shell/fda.py, hooked into
// the fs routes); until then this component renders nothing and quietly
// watches /api/config. An absent `fda` field (non-mac, dev server,
// inconclusive probe) means render nothing AND stop watching. `dismissed` is
// persisted server-side, so "Not now" is forever, not per-tab.
import { useEffect, useRef, useState } from "react";

import { dismissFdaNudge, getConfig, openFdaSettings } from "@platform/lib/api";
import { pushToast } from "@platform/lib/toast";

const WATCH_MS = 5000;
const GRANT_POLL_MS = 3000;

// hidden → (watching) → offer → waiting. "watching" renders nothing: the
// machine qualifies but this session hasn't touched a protected folder yet.
// "waiting" is System Settings open, instructions up. The grant landing is a
// success TOAST, not a card state — an achievement is transient, and the
// card's own title colour belongs to the invitation.
type Stage = "hidden" | "watching" | "offer" | "waiting";

export default function FdaCard() {
  const [stage, setStage] = useState<Stage>("hidden");
  const stageRef = useRef(stage);
  stageRef.current = stage;

  useEffect(() => {
    let disposed = false;
    getConfig()
      .then((config) => {
        if (disposed) return;
        const fda = config.fda;
        if (!fda || fda.granted || fda.dismissed) return;
        setStage(fda.relevant ? "offer" : "watching");
      })
      .catch(() => {}); // no config, no nudge — the server card owns outages
    return () => {
      disposed = true;
    };
  }, []);

  // While "watching", wait for the session's first protected-folder read.
  useEffect(() => {
    if (stage !== "watching") return;
    const timer = window.setInterval(() => {
      getConfig()
        .then((config) => {
          if (stageRef.current !== "watching") return;
          const fda = config.fda;
          if (!fda || fda.granted || fda.dismissed) setStage("hidden");
          else if (fda.relevant) setStage("offer");
        })
        .catch(() => {});
    }, WATCH_MS);
    return () => window.clearInterval(timer);
  }, [stage]);

  // While "waiting", watch for the grant landing (same-process grants only
  // apply after a relaunch in most cases — but when macOS relaunches the app
  // for us, the fresh server answers granted and a still-open tab converges).
  useEffect(() => {
    if (stage !== "waiting") return;
    const timer = window.setInterval(() => {
      getConfig()
        .then((config) => {
          if (stageRef.current !== "waiting") return;
          const fda = config.fda;
          if (fda?.granted) {
            setStage("hidden");
            // "info" is this app's confirmation tone — there is no green toast
            // (Toast.tsx), and every other success ("Path copied") is info too.
            pushToast({ msg: "Full Disk Access is on — no more prompts", tone: "info" });
          } else if (!fda || fda.dismissed) {
            // Same bail as the watching poll: the field vanishing or another
            // tab dismissing must not leave this card waiting forever.
            setStage("hidden");
          }
        })
        .catch(() => {});
    }, GRANT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [stage]);

  if (stage === "hidden" || stage === "watching") return null;

  const waiting = stage === "waiting";
  return (
    <div className="server-status server-status-fda" role="status" aria-live="polite">
      <div className="server-status-title">
        {waiting ? "In System Settings…" : "Give Full Disk Access"}
      </div>
      {waiting ? (
        <div className="server-status-body">Turn on FusedRender, then relaunch when asked.</div>
      ) : (
        <ol className="server-status-body fda-steps">
          <li>Open System Settings</li>
          <li>Privacy &amp; Security → Full Disk Access</li>
          <li>Turn on FusedRender, relaunch when asked</li>
        </ol>
      )}
      {waiting ? (
        <>
          <button
            type="button"
            className="server-status-retry server-status-fda-go"
            onClick={() => {
              openFdaSettings().catch(() => {});
            }}
          >
            Reopen System Settings
          </button>
          {/* The escape hatch: backing out of Settings without granting must
              not strand the card on "In System Settings…" for the session. */}
          <button
            type="button"
            className="server-status-retry"
            onClick={() => {
              setStage("hidden");
              dismissFdaNudge().catch(() => {});
            }}
          >
            Not now
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            className="server-status-retry server-status-fda-go"
            onClick={() => {
              setStage("waiting");
              openFdaSettings().catch(() => {});
            }}
          >
            Open System Settings
          </button>
          <button
            type="button"
            className="server-status-retry"
            onClick={() => {
              setStage("hidden");
              dismissFdaNudge().catch(() => {});
            }}
          >
            Not now
          </button>
        </>
      )}
    </div>
  );
}
