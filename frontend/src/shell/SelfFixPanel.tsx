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
  issueUrl,
  startSelfFix,
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
      navigateUrl(fixSessionUrl(started));
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
        Describe it and Claude will open a session on this installation to look.
        You do not need an error message — most of what goes wrong never raises
        one. You will land in the conversation and can watch it work; it asks
        before it changes anything.
      </p>
      <Field
        label="What is it doing?"
        hint="What you did, what you expected, what happened instead. The app's recent log goes along with it automatically."
      >
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
  onChanged,
}: {
  snapshot: SelfFixSnapshot;
  onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const { marker, reinstall } = snapshot;

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
          <p className="deploy-muted">
            Please send a report to the developers — a fix that only exists on
            this machine helps nobody else.
          </p>
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
                  onChanged();
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
      ) : (
        <p className="deploy-muted">
          Unmodified — this is the released build, exactly as it shipped.
        </p>
      )}

      {/* Reports with no marker: everything a dismissed badge left behind, and
          the sessions that changed nothing. Listing them here rather than only
          under a live badge is the difference between a record and a receipt —
          "did I already ask about this?" is answerable either way. */}
      {!marker && snapshot.reports.length > 0 && (
        <>
          <p className="deploy-muted">Earlier fix sessions left these reports:</p>
          <ul className="selffix-fixes">
            {snapshot.reports.map((r) => (
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
      .then((s) => alive && setSnapshot(s))
      .catch((e) => alive && setError(String((e as Error)?.message || e)));
    return () => {
      alive = false;
    };
  }, [epoch]);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!snapshot) return <SkeletonLines rows={4} label="Loading installation state" />;

  // Rendered in this order deliberately: the reason a user opened this tab is
  // almost always the first section, and the installation's state is context
  // for it — not the other way round.
  return (
    <>
      <DescribeSection writable={snapshot.writable} />
      <InstallationSection snapshot={snapshot} onChanged={() => setEpoch((n) => n + 1)} />
    </>
  );
}

export default SelfFixPanel;
