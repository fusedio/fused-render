// Share an app as a public link — the dialog behind every "Share" entry (the
// /apps card's hover chip and right-click menu, the app page header, the
// explorer kebab). One host (`ShareAppHost`, mounted once in the shell) renders
// it for whichever `openShareApp` request is current, so the menu entries that
// cannot own a dialog still get one (platform/lib/share-app.ts).
//
// What it says, and in which order (share_app.py owns what each call means):
//
//   • not signed in → the same "Sign in to Fused" button the Canvases page has,
//     polling /api/canvases/status until the login lands. Spelled here rather
//     than imported: platform may not import from apps/canvases.
//   • signed in, never shared → one primary, Share. On open a remote lookup
//     runs in the background (the app may have been shared from another
//     machine); Share does not wait for it — publishing adopts an existing
//     canvas by name anyway.
//   • shared → the link, Copy, Open, Update, Remove share. Remove is two
//     presses: the first arms it and says what it costs (every link handed out
//     stops working), the second does it.
//
// PUBLIC ONLY, by decision — there is no visibility control here. The one
// sentence under the link says so, because "anyone with the link" is the fact a
// reader needs before pasting it somewhere.
//
// Shared by the shell and the explorer, so it spells the two canvases routes it
// touches rather than importing either app's helpers.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";
import { getJson, postJson } from "@platform/lib/api";
import { copyToClipboard } from "@platform/lib/clipboard";
import { notify } from "@platform/lib/notifications";
import {
  closeShareApp,
  getShareStatus,
  lookupShare,
  publishShare,
  removeShare,
  useShareAppRequest,
  type ShareStatus,
  type SharedAppRecord,
} from "@platform/lib/share-app";
import type { ExportableApp } from "@platform/lib/appShot";
import { Modal } from "@platform/ui/modal/Modal";

const LOGIN_POLL_MS = 1500;
const LUCIDE = { size: 14, strokeWidth: 1.75, "aria-hidden": true } as const;

interface CanvasesStatusLite {
  logged_in: boolean;
  creds_stamp: number | null;
  login_in_flight: boolean;
}

type Busy = null | "publish" | "update" | "remove" | "login";

export function ShareAppModal({
  app,
  captureEl,
  onClose,
}: {
  app: ExportableApp;
  captureEl: Element | null;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<ShareStatus | null>(null);
  const [shared, setShared] = useState<SharedAppRecord | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [err, setErr] = useState("");
  const [copied, setCopied] = useState(false);
  const [armed, setArmed] = useState(false);
  const [looking, setLooking] = useState(false);
  const loginStampRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getShareStatus(app.path);
      setStatus(s);
      setShared(s.shared);
      return s;
    } catch (e) {
      setErr((e as Error).message);
      return null;
    }
  }, [app.path]);

  // First read, then — when signed in with no local record — ask Fused once
  // whether a canvas for this app already exists somewhere.
  useEffect(() => {
    let cancelled = false;
    void refresh().then((s) => {
      if (cancelled || !s || !s.logged_in || s.shared || !s.can_share || !s.app_id) return;
      setLooking(true);
      lookupShare(app.path)
        .then((r) => {
          if (!cancelled && r.found && r.shared) setShared(r.shared);
        })
        .catch(() => {
          /* a failed lookup is not an error worth a sentence — Share still works */
        })
        .finally(() => {
          if (!cancelled) setLooking(false);
        });
    });
    return () => {
      cancelled = true;
    };
  }, [app.path, refresh]);

  useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    },
    [],
  );

  const onLogin = async () => {
    if (busy) return;
    setErr("");
    setBusy("login");
    loginStampRef.current = status?.creds_stamp ?? null;
    try {
      await postJson<{ ok: boolean }>("/api/canvases/login", {});
    } catch (e) {
      setBusy(null);
      setErr((e as Error).message);
      return;
    }
    pollRef.current = window.setInterval(() => {
      void getJson<CanvasesStatusLite>("/api/canvases/status").then((s) => {
        const completed = s.logged_in && s.creds_stamp !== loginStampRef.current;
        if (completed) {
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setBusy(null);
          void refresh();
        } else if (!s.login_in_flight) {
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setBusy(null);
          setErr("Sign-in was not completed — try again.");
        }
      });
    }, LOGIN_POLL_MS);
  };

  const doPublish = async (kind: "publish" | "update") => {
    if (busy) return;
    setErr("");
    setArmed(false);
    setBusy(kind);
    try {
      const rec = await publishShare(app, captureEl);
      setShared(rec);
      notify({ title: kind === "update" ? "Share updated" : "Link ready", tone: "info" });
    } catch (e) {
      const error = e as Error & { code?: string };
      if (error.code === "not_logged_in") {
        void refresh();
      }
      setErr(error.message);
    } finally {
      setBusy(null);
    }
  };

  const doRemove = async () => {
    if (busy) return;
    if (!armed) {
      setArmed(true);
      return;
    }
    setErr("");
    setBusy("remove");
    try {
      await removeShare(app.path);
      setShared(null);
      setArmed(false);
      notify({ title: "Share removed", tone: "info" });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const copy = async () => {
    if (!shared?.url) return;
    const ok = await copyToClipboard(shared.url);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  const working = busy === "publish" || busy === "update" || busy === "remove";
  const title = `Share ${app.name}`;

  let body: ReactNode;
  if (!status && !err) {
    body = <p className="deploy-muted">Checking…</p>;
  } else if (status && !status.can_share) {
    body = <p className="deploy-error" role="alert">{status.refusal}</p>;
  } else if (status && !status.cli_found) {
    body = (
      <p>
        The fused CLI is not available in this server&rsquo;s environment. Install it with{" "}
        <code>pip install &quot;fused-render[fused]&quot;</code>.
      </p>
    );
  } else if (status && !status.logged_in) {
    body = (
      <>
        <p>
          Sharing publishes this app to your Fused account as a public page. Sign in to
          Fused first.
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={onLogin}
            disabled={busy === "login"}
          >
            {busy === "login" ? "Waiting for browser sign-in…" : "Sign in to Fused"}
          </button>
          {busy === "login" && (
            <span className="deploy-muted">Complete the sign-in in the browser window that just opened.</span>
          )}
        </div>
      </>
    );
  } else if (!shared) {
    body = (
      <>
        <p>
          Publishes <b>{app.name}</b> as a <code>.fused</code> app file on a public page:
          anyone with the link can see the app&rsquo;s README and download it. Sharing
          again later updates the same link.
        </p>
        {looking && <p className="deploy-muted">Checking whether it is already shared…</p>}
        {busy === "publish" && (
          <p className="deploy-muted">Exporting and uploading… this can take up to a minute.</p>
        )}
      </>
    );
  } else {
    body = (
      <>
        <div className="share-app-link">
          <input
            type="text"
            readOnly
            value={shared.url ?? "No public link yet — press Update to publish one."}
            onFocus={(e) => e.currentTarget.select()}
            aria-label="Public link"
          />
          <button
            type="button"
            className="btn btn-secondary"
            onClick={copy}
            disabled={!shared.url}
            title="Copy link"
          >
            {copied ? <Check {...LUCIDE} /> : <Copy {...LUCIDE} />}
            {copied ? "Copied" : "Copy"}
          </button>
          {shared.url && (
            <a
              className="btn btn-secondary"
              href={shared.url}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the shared page"
            >
              <ExternalLink {...LUCIDE} />
              Open
            </a>
          )}
        </div>
        <p className="deploy-muted">
          Public: anyone with the link can open it.
          {shared.adopted
            ? " This share was found on your account; press Update to publish the current folder."
            : shared.updated_at
              ? ` Last published ${new Date(shared.updated_at * 1000).toLocaleString()}.`
              : ""}
          {shared.workbench_url && (
            <>
              {" "}
              <a href={shared.workbench_url} target="_blank" rel="noopener noreferrer">
                Open in Workbench
              </a>
            </>
          )}
        </p>
        {busy === "update" && (
          <p className="deploy-muted">Exporting and uploading… this can take up to a minute.</p>
        )}
        {armed && !working && (
          <p className="deploy-error" role="alert">
            Removing the share deletes the page and its canvas. Every link already sent
            stops working. Press Remove share again to confirm.
          </p>
        )}
      </>
    );
  }

  const footer =
    status && status.can_share && status.cli_found && status.logged_in ? (
      shared ? (
        <>
          <button
            type="button"
            className={armed ? "btn btn-danger" : "btn btn-secondary"}
            disabled={working}
            onClick={doRemove}
          >
            {busy === "remove" ? "Removing…" : armed ? "Remove share" : "Remove share"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={working}
            onClick={() => void doPublish("update")}
          >
            {busy === "update" ? "Updating…" : "Update"}
          </button>
        </>
      ) : (
        <>
          <button type="button" className="btn btn-secondary" disabled={working} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={working}
            onClick={() => void doPublish("publish")}
          >
            {busy === "publish" ? "Sharing…" : "Share"}
          </button>
        </>
      )
    ) : (
      <button type="button" className="btn btn-secondary" onClick={onClose}>
        Close
      </button>
    );

  return (
    <Modal title={title} busy={working} onClose={onClose} width={520} footer={footer}>
      {body}
      {err && (
        <p className="deploy-error" role="alert">
          {err}
        </p>
      )}
    </Modal>
  );
}

/** Mounted once in the shell; renders the dialog for the current request. */
export function ShareAppHost() {
  const req = useShareAppRequest();
  if (!req) return null;
  return (
    <ShareAppModal
      key={req.seq}
      app={req.app}
      captureEl={req.captureEl}
      onClose={closeShareApp}
    />
  );
}

export default ShareAppModal;
