// Preferences → "Fix this app" (SPEC §42, SF-14). The self-fix feature's HOME,
// as opposed to its notification.
//
// **Why a tab and not only the failed-row button.** The download manager's
// "Fix this" answers "this went wrong just now"; a great deal of what is
// actually wrong with an app never raises anything. The preview renders the
// wrong dates, a folder takes ten seconds to open, a template's button does
// nothing. There is no error, so there is no row, so there was no way in at all
// — and the user is left with the option this feature exists to replace:
// describing it to us and waiting for a release.
//
// So this tab takes the one thing that surface cannot: a sentence. The server
// then has no traceback to hand the session, which is why the incident it
// writes carries the app log inline and the prompt tells the session to
// REPRODUCE before it diagnoses (fused_render/selffix.py) — a described problem
// nobody can observe is a question, not a diagnosis.
//
// It is also where the installation's own state belongs. The version chip's
// popover has to be small and is a notification: something happened, here is
// the report. This is the full-size account — every report ever written, the
// reinstall instructions, the dismiss — with room to read it.
import { useEffect, useState } from "react";

import { navigate, navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Field, TextArea } from "@platform/ui/field/fields";
import {
  clearSelfFix,
  describedProblemIsSendable,
  failureContextFromNote,
  fixSessionUrl,
  getSelfFix,
  isSelfFixStorageKey,
  issueUrl,
  startSelfFix,
  unlistedReports,
  SELFFIX_CHANGED_EVENT,
  type SelfFixSnapshot,
} from "@platform/lib/selffix";

function when(at: number): string {
  return new Date(at * 1000).toLocaleString();
}

function basename(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

// "Describe what's wrong" — the entry point that needs no error to have
// happened. Deliberately a free-text box and not a form: the user does not know
// which subsystem is at fault (that is the session's job to find out), and a
// dropdown of our subsystem names would ask them to guess it before they are
// allowed to ask for help.
function DescribeSection({ writable }: { writable: boolean }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const started = await startSelfFix(failureContextFromNote(note));
      // The target is ALWAYS the install directory, so the hint is a fact and
      // not a guess — the same one the scaffolder's identical handoff passes
      // (builder/HomeHero). Without it the explorer paints file chrome until
      // `stat` answers, which is a visible stutter on the one navigation this
      // feature promises lands you in the session (SF-3).
      navigateUrl(fixSessionUrl(started), { isDir: true });
    } catch (e) {
      // Covers the two refusals worth reading: a read-only installation, and a
      // fix session already running (one at a time — they all edit the same
      // tree). Both are the server's own sentence; neither is worth rewording.
      setError(String((e as Error)?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Something wrong with the app?</h2>
      <p className="deploy-muted">
        Describe what went wrong and Claude will open a session on this
        installation to look. You do not necessarily need an error message. You
        will land in the Claude session and can watch it work.
      </p>
      <Field label="What is going on?">
        <TextArea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={4}
          placeholder={
            "e.g. Opening a folder with a few thousand files takes about ten " +
            "seconds and the window is frozen the whole time. It was quick last week."
          }
        />
      </Field>
      {!writable && (
        <ErrorBanner>
          This installation is read-only, so a fix cannot be applied to it. See
          the reinstall instructions below, or install fused-render somewhere you
          own.
        </ErrorBanner>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <button
        type="button"
        onClick={start}
        disabled={busy || !writable || !describedProblemIsSendable(note)}
      >
        {busy ? "Starting…" : "Start a fix session"}
      </button>
    </section>
  );
}

// The installation's own account: modified or stock, every report, how to get
// a clean copy back. The version chip shows a summary of this; here it has room.
function InstallationSection({
  snapshot,
  onDismissed,
}: {
  snapshot: SelfFixSnapshot;
  /** Drop the marker locally — see the Dismiss button. */
  onDismissed: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const { marker, reinstall } = snapshot;
  const otherReports = unlistedReports(snapshot.reports, marker);

  return (
    <section className="prefs-section">
      <h2>This installation</h2>
      <p className="deploy-muted">
        fused-render <strong>v{snapshot.version}</strong>, installed at{" "}
        <code>{snapshot.install_root}</code>.
      </p>

      {marker ? (
        <>
          <p className="selffix-modified">
            Modified — a fix session changed files here, most recently{" "}
            {marker.modified_at ? when(marker.modified_at) : "recently"}. This copy
            is no longer the released one.
          </p>
          <ul className="selffix-fixes">
            {[...marker.fixes].reverse().map((fix) => (
              <li key={fix.run_id || String(fix.at)}>
                <span className="selffix-fix-title">
                  {fix.title || "a problem the user described"}
                </span>
                <span className="deploy-muted"> — {when(fix.at)}</span>
                {fix.report && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="selffix-report"
                      onClick={() => navigate(fix.report as string, { isDir: false })}
                    >
                      {basename(fix.report)}
                    </button>
                  </>
                )}
              </li>
            ))}
          </ul>
          <div className="version-panel-actions">
            <a
              className="update-badge-action"
              href={issueUrl(snapshot)}
              target="_blank"
              rel="noreferrer"
            >
              Open an issue ↗
            </a>
            <button
              type="button"
              onClick={async () => {
                try {
                  await clearSelfFix();
                  // PATCHED, not re-fetched — and the difference is the whole
                  // point. The nudge `clearSelfFix` fires is what RE-READS, and
                  // adding a second re-read here would be a second path to one
                  // answer. But a re-read can fail, and this tab now keeps the
                  // last good snapshot when it does, so relying on it alone
                  // left the section saying "Modified" beside a chip that had
                  // already gone clean. The server has just confirmed the
                  // clear, so the marker is known-gone: say so now and let the
                  // nudge's read confirm it. The version chip's own dismiss has
                  // always worked this way, for this reason.
                  onDismissed();
                } catch (e) {
                  setError(String((e as Error)?.message || e));
                }
              }}
            >
              Dismiss the modified badge
            </button>
          </div>
          <p className="deploy-muted">
            Dismissing clears the badge only — the reports above stay on disk.
          </p>
        </>
      ) : null}
      {/* NOTHING IS SAID WHEN THERE IS NO MARKER, and the silence is the point.
          This used to read "Unmodified — this is the released build, exactly as
          it shipped", which is a claim about BYTES that no marker can support:
          Dismiss deliberately clears the mark and keeps the patch (SF-15), so
          the very next snapshot took a dismissed installation — still carrying
          Claude's changes — and told the user it was pristine. What the app
          actually knows is provenance, not integrity (SF-7a): it can say a
          self-fix session changed this copy, and it cannot say that nothing
          did. A weaker sentence ("no badge is active") would only be a quieter
          version of answering a question we cannot answer, so the section says
          what it knows — the version, the path, the reports — and stops. */}

      {/* EVERY OTHER REPORT ON DISK, whether or not a badge is up. This used to
          be shown only when there was no marker, which hid exactly the reports
          it exists to keep: the marker's `fixes` is CAPPED (selffix.MAX_FIXES),
          a dismiss drops the marker while keeping the files, and a session that
          changed nothing still writes one — so while a badge was active, all
          three were invisible. `list_reports` reads the DIRECTORY for precisely
          that reason ("a listing that could go missing under a cap would be the
          one thing this feature is not allowed to lose"), and the UI was
          contradicting its own server.

          Filtered against the fixes above rather than shown twice: a report
          already named by a titled row says more there than it would as a bare
          filename here. So the rule is one line — every report is reachable,
          none appears twice — instead of a condition on the marker. */}
      {otherReports.length > 0 && (
        <>
          <p className="deploy-muted">
            {marker ? "Other reports on disk:" : "Earlier fix sessions left these reports:"}
          </p>
          <ul className="selffix-fixes">
            {otherReports.map((r) => (
              <li key={r.path}>
                <button
                  type="button"
                  className="selffix-report"
                  onClick={() => navigate(r.path, { isDir: false })}
                >
                  {r.name}
                </button>
                <span className="deploy-muted"> — {when(r.at)}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h2>{reinstall.headline}</h2>
      <p className="deploy-muted">{reinstall.note}</p>
      {reinstall.command && (
        <div className="update-badge-command">
          <code>{reinstall.command}</code>
          <button
            type="button"
            className="update-badge-copy"
            onClick={() => navigator.clipboard.writeText(reinstall.command)}
          >
            Copy
          </button>
        </div>
      )}
      <p>
        <a
          className={reinstall.command ? "version-panel-link" : "update-badge-action"}
          href={reinstall.url}
          target="_blank"
          rel="noreferrer"
        >
          {reinstall.url_label ?? reinstall.url} ↗
        </a>
      </p>
      <p className="deploy-muted">
        Reinstalling always clears the modified badge: the record lives inside
        the installation folder, so replacing that folder removes it.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

export function SelfFixPanel() {
  const [snapshot, setSnapshot] = useState<SelfFixSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    let alive = true;
    getSelfFix()
      .then((s) => {
        if (!alive) return;
        setSnapshot(s);
        // CLEARED ON SUCCESS, or the banner outlives the failure that raised
        // it: the read retries on every nudge, and an `error` that only ever
        // gets set left the tab showing "could not load" over a server that had
        // been answering for the last ten minutes. A message about a request is
        // only true until the next one.
        setError(null);
      })
      .catch((e) => alive && setError(String((e as Error)?.message || e)));
    return () => {
      alive = false;
    };
  }, [epoch]);

  // RE-READ ON THE NUDGE, exactly as the version chip does, because this tab is
  // the other half of the same state and it was the only surface not listening.
  // A dismiss from the chip's panel, or a session stamping the install from
  // another tab, left Preferences asserting the opposite of what the sidebar
  // showed until the tab was remounted — the disagreeing-surfaces failure the
  // nudge exists to prevent, arriving through the one door that had no listener.
  //
  // The re-read is the WHOLE of the response, so this needs no epoch guard of
  // its own: bumping `epoch` re-runs the effect above, whose cleanup drops the
  // previous fetch's `alive` flag. React's own teardown is the guard the chip
  // has to hand-roll (it owns a timer chain; this owns one fetch).
  useEffect(() => {
    const refresh = () => setEpoch((n) => n + 1);
    const onPing = (e: StorageEvent) => {
      if (isSelfFixStorageKey(e.key)) refresh();
    };
    window.addEventListener("storage", onPing);
    window.addEventListener(SELFFIX_CHANGED_EVENT, refresh);
    return () => {
      window.removeEventListener("storage", onPing);
      window.removeEventListener(SELFFIX_CHANGED_EVENT, refresh);
    };
  }, []);

  // An error takes the WHOLE tab only when there is nothing else to show. Once
  // a snapshot has landed, a later failed re-read is a note above content that
  // is still broadly true, not a reason to replace the entire tab with a
  // sentence — the nudge fires this read on every state change, so one blip
  // (the server restarting mid-fix, which is a thing fix sessions cause) would
  // otherwise take away the reinstall instructions and the report list.
  if (!snapshot) {
    return error ? (
      <ErrorBanner>{error}</ErrorBanner>
    ) : (
      <SkeletonLines rows={4} label="Loading installation state" />
    );
  }

  // Rendered in this order deliberately: the reason a user opened this tab is
  // almost always the first section, and the installation's state is context
  // for it — not the other way round.
  return (
    <>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <DescribeSection writable={snapshot.writable} />
      <InstallationSection
        snapshot={snapshot}
        onDismissed={() =>
          setSnapshot((s) => (s ? { ...s, modified: false, marker: null } : s))
        }
      />
    </>
  );
}

export default SelfFixPanel;
