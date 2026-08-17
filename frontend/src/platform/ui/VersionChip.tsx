// The sidebar's version indicator — and, when this installation has been
// changed by a self-fix session, the one place that says so (SPEC §42, SF-6).
//
// **Why the version, and not a badge of its own.** "v0.4.18" is a claim: it
// names the bytes you are running, and every support conversation starts by
// asking for it. Once a Claude session has edited this install, that claim is
// no longer true — the number is the release this copy *came from*, not what it
// *is*. So the correction belongs on the number rather than beside it: a
// separate badge would leave a confident, wrong version string sitting next to
// it, and the two would be read in either order.
//
// Amber rather than red on purpose. Nothing is broken — a fix was applied, very
// possibly successfully. What the chip says is "this is not stock", which is a
// caveat, not an error.
//
// Clicking it opens the panel, which has to answer three questions in the order
// people actually ask them: what happened (→ the report), who needs to know
// (→ the developers), and how do I get back to a normal copy (→ reinstall).
import React, { useEffect, useRef, useState } from "react";

import { getConfig, rawUrl } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import {
  clearSelfFix,
  getSelfFix,
  issueUrl,
  lastFixStartedAt,
  modifiedSummary,
  isSelfFixStorageKey,
  selffixPollInterval,
  SELFFIX_CHANGED_EVENT,
  type ModifiedInstall,
  type SelfFixSnapshot,
} from "@platform/lib/selffix";

// Kept in step with `.version-panel`'s width in styles/sidebar.css — the
// viewport clamp below has to know how wide the thing it is placing is, and a
// panel measured after paint would place itself twice.
const PANEL_WIDTH = 300;

function basename(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

// One control per action, and the label is the state: `resolve` may be async
// (the report has to be read off disk first), so the button is what waits, not
// a second control the user has to find and press in the right order.
function CopyButton({
  resolve,
  label = "Copy",
  onError,
}: {
  resolve: () => string | Promise<string>;
  label?: string;
  onError?: (message: string) => void;
}) {
  const [state, setState] = useState<"idle" | "busy" | "copied">("idle");
  return (
    <button
      type="button"
      className="update-badge-copy"
      disabled={state === "busy"}
      onClick={async () => {
        setState("busy");
        try {
          await navigator.clipboard.writeText(await resolve());
        } catch (e) {
          // No clipboard permission, or the file could not be read. Both are
          // worth saying: a Copy button that silently did nothing is the one
          // failure the user will retry forever.
          setState("idle");
          onError?.(String((e as Error)?.message || e));
          return;
        }
        setState("copied");
        window.setTimeout(() => setState("idle"), 2000);
      }}
    >
      {state === "copied" ? "Copied" : state === "busy" ? "Copying…" : label}
    </button>
  );
}

// The panel's body. Split out so the chip renders instantly from the config
// field it already has, and the expensive half (a directory listing plus, on a
// mac, a `brew list` subprocess — see routers/selffix.py) is fetched only once
// the user has actually opened it.
function ModifiedPanel({
  marker,
  at,
  onClose,
  onCleared,
}: {
  marker: ModifiedInstall;
  /** Viewport coordinates for the fixed-position panel — see `openAt`. */
  at: { left: number; top: number };
  onClose: () => void;
  /** Drop the badge NOW rather than at the next poll — see the Dismiss row. */
  onCleared: () => void;
}) {
  const [snapshot, setSnapshot] = useState<SelfFixSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    getSelfFix()
      .then((s) => !disposed && setSnapshot(s))
      .catch((e) => !disposed && setError(String(e?.message || e)));
    return () => {
      disposed = true;
    };
  }, []);

  const report = marker.latest_report ?? snapshot?.reports[0]?.path ?? null;

  // Read off disk only when the Copy button is pressed. The panel never RENDERS
  // the report — it is a document, and a document belongs in the app's own
  // markdown view (that is what "Open the report" is for), not inside a 300px
  // popover.
  const readReport = async (): Promise<string> => {
    const res = await fetch(rawUrl(report as string));
    if (!res.ok) throw new Error("Could not read the report file.");
    return res.text();
  };

  const openReport = () => {
    if (!report) return;
    onClose();
    navigate(report, { isDir: false });
  };

  const reinstall = snapshot?.reinstall;

  return (
    <div
      className="version-panel"
      role="dialog"
      aria-label="Modified installation"
      style={{ left: at.left, top: at.top }}
    >
      <div className="version-panel-head">Modified installation</div>
      <div className="update-badge-text">{modifiedSummary(marker)}</div>
      <div className="update-badge-text">
        You are running <strong>v{marker.version}</strong> with local changes on
        top — this copy is no longer the released one.
      </div>

      <div className="version-panel-section">
        <div className="version-panel-label">What was changed</div>
        {report ? (
          <>
            <button type="button" className="update-badge-action" onClick={openReport}>
              Open the report
            </button>
            <div className="version-panel-file" title={report}>
              {basename(report)}
            </div>
          </>
        ) : (
          <div className="update-badge-text">
            The session left no report — which should not happen. The files it
            changed are under <code>{marker.install_root}</code>.
          </div>
        )}
      </div>

      <div className="version-panel-section">
        <div className="version-panel-label">Tell the developers</div>
        <div className="update-badge-text">
          Send the report to the Fused Render developers so it can ship for
          everyone.
        </div>
        <div className="version-panel-actions">
          {snapshot && (
            <a
              className="update-badge-action"
              href={issueUrl(snapshot)}
              target="_blank"
              rel="noreferrer"
            >
              Open an issue ↗
            </a>
          )}
          {report && (
            <CopyButton resolve={readReport} label="Copy report" onError={setError} />
          )}
        </div>
      </div>

      <div className="version-panel-section">
        <div className="version-panel-label">
          {reinstall?.headline ?? "Reinstall the latest version"}
        </div>
        <div className="update-badge-text">
          {reinstall?.note ??
            "Reinstalling replaces this copy with the released one."}
        </div>
        {reinstall?.command && (
          <div className="update-badge-command">
            <code>{reinstall.command}</code>
            <CopyButton resolve={() => reinstall.command} onError={setError} />
          </div>
        )}
        {/* NO COMMAND MEANS THE LINK IS THE INSTRUCTION, so it is styled as the
            section's action rather than as a footnote. That is the DMG case —
            the most common end-user install, where reinstalling is "go to the
            download page and drag it over" and there is nothing to type. Left
            as a quiet link where a command carries the actual instruction
            (brew, pip, git), so the section never has two primary actions. */}
        {reinstall?.url && (
          <a
            className={
              reinstall.command ? "version-panel-link" : "update-badge-action"
            }
            href={reinstall.url}
            target="_blank"
            rel="noreferrer"
          >
            {reinstall.url_label ?? reinstall.url} ↗
          </a>
        )}
        {/* The promise this whole feature rests on, said out loud: the mark
            lives INSIDE the installation, so replacing the installation takes
            it with it. Nobody has to remember to clear anything. */}
        <div className="update-badge-text">
          Reinstalling always clears this badge.
        </div>
      </div>

      {error && <div className="update-badge-text update-badge-error">{error}</div>}

      {/* Last, and quiet: the badge is a claim about this machine, and the
          person at it is allowed to settle it — they may have reverted the
          change by hand, or decided to keep it. Clears the mark only; the
          report files stay.

          The chip goes on the SERVER's 200, not on the next poll: at the idle
          cadence that is up to a minute of an amber badge sitting there after
          the user dismissed it, which reads as the dismiss having failed. The
          poll still reconciles — this only removes the gap (the download
          manager's `patch`, same argument). */}
      <button
        type="button"
        className="version-panel-link"
        onClick={async () => {
          try {
            await clearSelfFix();
            onCleared();
          } catch (e) {
            setError(String((e as Error)?.message || e));
            return;
          }
          onClose();
        }}
      >
        Dismiss this badge (keeps the report)
      </button>
    </div>
  );
}

// Keep `modified_install` live without a page reload. Seeded from the config
// the sidebar already has, then re-read on the two-speed cadence lib/selffix
// describes — slow always, fast while a fix session this browser started is
// plausibly still running.
//
// Owns its own poll rather than riding ServerStatusBanner's 5s one, exactly
// like UpdateBadge and for the same two reasons: the components live in
// different trees, and this state changes about once in the life of an install.
function useModifiedInstall(
  seed: ModifiedInstall | null | undefined
): [ModifiedInstall | null, (next: ModifiedInstall | null) => void] {
  const [modified, setModified] = useState<ModifiedInstall | null>(seed ?? null);

  // The prop is the shell's own answer (the config it booted with, or refetched
  // after a restart) and it wins whenever it changes — a poll that has not fired
  // yet must not hold a stale badge over a fresher prop.
  useEffect(() => {
    setModified(seed ?? null);
  }, [seed]);

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const config = await getConfig();
        if (!disposed) setModified(config.modified_install ?? null);
      } catch {
        // Server unreachable — ServerStatusBanner owns that story; keep the
        // last answer rather than blanking a badge on one failed probe.
      }
      if (disposed) return;
      timer = window.setTimeout(poll, selffixPollInterval(lastFixStartedAt()));
    }

    // Self-fix state changed somewhere — a fix started, or a badge was
    // dismissed: re-arm NOW so the answer changes immediately instead of after
    // the running idle timer expires.
    //
    // Two listeners for one nudge, because one channel cannot carry it (see
    // lib/selffix). Both firing is harmless: `rearm` cancels the pending timer
    // before it polls.
    const rearm = () => {
      window.clearTimeout(timer);
      poll();
    };
    const onPing = (e: StorageEvent) => {
      if (isSelfFixStorageKey(e.key)) rearm();
    };
    timer = window.setTimeout(poll, selffixPollInterval(lastFixStartedAt()));
    window.addEventListener("storage", onPing);
    window.addEventListener(SELFFIX_CHANGED_EVENT, rearm);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      window.removeEventListener("storage", onPing);
      window.removeEventListener(SELFFIX_CHANGED_EVENT, rearm);
    };
  }, []);

  return [modified, setModified];
}

export default function VersionChip({
  version,
  modified: seed,
}: {
  version?: string;
  /** /api/config's `modified_install`, or null on a stock installation. */
  modified?: ModifiedInstall | null;
}) {
  const [modified, setModified] = useModifiedInstall(seed);
  // Viewport coordinates, or null for shut. The panel is `position: fixed` and
  // measured off the chip rather than absolutely positioned inside it, because
  // `#sidebar` scrolls (`overflow-y: auto`) and clips horizontally
  // (`overflow-x: hidden`) — an absolute child would be a 232px-wide sliver
  // that scrolled away with the nav. The same trap the schedule modal's
  // dropdowns hit (D304); the same fix, and the same one the sidebar's own
  // Settings menu already uses.
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  const rootRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!at) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setAt(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setAt(null);
    };
    // A fixed panel does not follow its trigger, so anything that MOVES the
    // trigger shuts it rather than leaving it stranded: a sidebar scroll, a
    // resize, a collapse, a window blur.
    const onMoved = () => setAt(null);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onMoved);
    window.addEventListener("blur", onMoved);
    document.getElementById("sidebar")?.addEventListener("scroll", onMoved);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onMoved);
      window.removeEventListener("blur", onMoved);
      document.getElementById("sidebar")?.removeEventListener("scroll", onMoved);
    };
  }, [at]);

  if (!version) return null;
  // Stock install: a plain label, exactly as before. Not a button — there is
  // nothing behind it, and a control that opens nothing is worse than text.
  if (!modified) return <span className="brand-version">v{version}</span>;

  const openAt = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (at) {
      setAt(null);
      return;
    }
    const r = e.currentTarget.getBoundingClientRect();
    // Clamped to the viewport: the sidebar is draggable and the window can be
    // narrow, so "just below the chip" is not automatically on screen.
    setAt({
      left: Math.max(8, Math.min(r.left, window.innerWidth - PANEL_WIDTH - 8)),
      top: r.bottom + 6,
    });
  };

  return (
    <span className="brand-version-host" ref={rootRef}>
      <button
        type="button"
        className="brand-version is-modified"
        aria-expanded={at !== null}
        aria-haspopup="dialog"
        // The accessible name says what the colour says, because the colour is
        // the whole signal and a screen reader gets none of it. `title` repeats
        // it for the pointer.
        aria-label={`Version ${version} — this installation has been modified locally. Open the report.`}
        title={`v${version} — this installation has been modified locally. Click for the report.`}
        onClick={openAt}
      >
        v{version}
        {/* A mark, not only a colour: the chip is 10.5px muted text, and colour
            alone is not a signal everyone can read. */}
        <span className="brand-version-mark" aria-hidden="true">
          ✳
        </span>
      </button>
      {at && (
        <ModifiedPanel
          marker={modified}
          at={at}
          onClose={() => setAt(null)}
          onCleared={() => setModified(null)}
        />
      )}
    </span>
  );
}
