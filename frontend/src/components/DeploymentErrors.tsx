// Recent captured failures for one deployed mount — the owner-only view behind
// a deployed page's opaque 500s (`fused share errors`, the fused repo's
// error-reporting.md). Loads its newest-first list on mount, so a parent
// controls laziness by mounting it only when wanted: the Deploy modal renders
// one per deployed page (auto-loads), the account Deployments list mounts one
// per row only when its "Recent errors" panel is opened (no CLI call per row
// until asked). Clicking a row fetches and expands the full record — the
// traceback, output tails, and the params that triggered it. Nothing here is
// ever shown to the deployed page's own viewers.
import { useEffect, useRef, useState } from "react";
import {
  getDeployErrorDetail,
  listDeployErrors,
  type DeployErrorRecord,
  type DeployErrorSummary,
} from "../lib/api";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// The command the hosted overlay also names — copyable so an owner can pull the
// full record in a terminal too. `--env` targets the same named environment the
// server itself uses here (deploy.py's `_run_share` sets OPENFUSED_ENV for the
// child process) — without it, a bare `fused share errors` falls back to
// whatever environment happens to be ambient/default in the terminal it's
// pasted into, which can silently query the wrong environment (or none) when
// inspecting a deployment on a non-default one. The `--` before the
// positionals mirrors the API path (`_errors_args` in deploy.py): it stops a
// token/err_id beginning with '-' from being parsed as a Click option, so the
// pasted command behaves identically to the UI.
function cliCommand(env: string, token: string, errId: string): string {
  return `fused --env ${env} share errors -- ${token} ${errId}`;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="deploy-err-field">
      <div className="deploy-err-field-label">{label}</div>
      <pre className="deploy-err-pre">{value}</pre>
    </div>
  );
}

function ErrorDetail({
  env,
  token,
  errId,
}: {
  env: string;
  token: string;
  errId: string;
}) {
  const [record, setRecord] = useState<DeployErrorRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    getDeployErrorDetail(env, token, errId)
      .then((res) => alive && setRecord(res.record))
      .catch((e) => alive && setError((e as Error).message))
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [env, token, errId]);

  if (loading) return <div className="deploy-muted">Loading record…</div>;
  // A miss is an expected state (expired, rate-cap drop, or an id minted before
  // capture) — the server passes the CLI's plain message through.
  if (error) return <div className="deploy-muted">{error}</div>;
  if (!record) return null;

  const params =
    // `!= null` (not `!== undefined`): the managed control plane serializes an
    // absent params field as JSON null, the AWS plane omits the key entirely.
    // Both must fall through to params_preview rather than render "null".
    record.params != null
      ? JSON.stringify(record.params, null, 2)
      : record.params_preview;

  return (
    <div className="deploy-err-detail">
      {record.error && <Field label="Error" value={record.error} />}
      {record.stdout_tail && <Field label="stdout (tail)" value={record.stdout_tail} />}
      {record.stderr_tail && <Field label="stderr (tail)" value={record.stderr_tail} />}
      {params && (
        <Field
          label={record.params_truncated ? "Params (preview — truncated)" : "Params"}
          value={params}
        />
      )}
      {record.truncated && (
        <div className="deploy-muted deploy-err-note">
          Some fields were truncated at capture to bound the record size.
        </div>
      )}
    </div>
  );
}

export default function DeploymentErrors({ env, token }: { env: string; token: string }) {
  const [errors, setErrors] = useState<DeployErrorSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const load = async () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const res = await listDeployErrors(env, token, { limit: 10 });
      if (seq !== loadSeq.current) return;
      setErrors(res.errors);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError((e as Error).message);
      setErrors(null);
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    setErrors(null);
    setError(null);
    setOpenId(null);
    void load();
    return () => {
      // Invalidate any in-flight fetch so its result can't land on the next mount.
      loadSeq.current++;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [env, token]);

  return (
    <div className="deploy-errors">
      <div className="deploy-errors-head">
        <span className="deploy-errors-title">Recent errors</span>
        <button type="button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </div>
      {loading && <div className="deploy-muted">Loading recent errors…</div>}
      {error && <div className="deploy-error">{error}</div>}
      {errors && errors.length === 0 && (
        <div className="deploy-muted">
          No errors captured recently — this deployment's endpoints have not
          failed (within the retention window), or capture was rate-capped.
        </div>
      )}
      {errors && errors.length > 0 && (
        <ul className="deploy-err-list">
          {errors.map((e) => {
            const open = openId === e.err_id;
            return (
              <li key={e.err_id} className={"deploy-err-item" + (open ? " open" : "")}>
                <button
                  type="button"
                  className="deploy-err-row"
                  onClick={() => setOpenId(open ? null : e.err_id)}
                  aria-expanded={open}
                >
                  <span className="deploy-err-time">{fmtTime(e.occurred_at)}</span>
                  <span className={"deploy-err-kind kind-" + e.kind}>{e.kind}</span>
                  {e.entrypoint && <span className="deploy-err-entry">{e.entrypoint}</span>}
                  <span className="deploy-err-msg" title={e.error}>
                    {e.error || "(no message)"}
                  </span>
                </button>
                {open && (
                  <div className="deploy-err-body">
                    <div className="deploy-err-cmd">
                      <code>{cliCommand(env, token, e.err_id)}</code>
                      <button
                        type="button"
                        onClick={() => {
                          void navigator.clipboard?.writeText(cliCommand(env, token, e.err_id));
                        }}
                      >
                        Copy command
                      </button>
                    </div>
                    <ErrorDetail env={env} token={token} errId={e.err_id} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
