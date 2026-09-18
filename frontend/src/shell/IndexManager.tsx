// Index — the /index sentinel, entered from the sidebar's own
// "Index" entry (GlobalSidebar) or the /index URL directly. The
// management page SPEC-index-plugins.md's message one asked for: "I want to
// make this more modular and accessible using a new page."
//
// **What lives here, and why nothing else does:**
//
// - Every registered index KIND (GET /api/index/kinds — "files" plus
//   whatever third-party/example kinds are registered, "apps" always among
//   them) with its own status, Scan/Full scan/Delete controls. This is the
//   one place a non-"files" kind can be seen or managed at all; nothing in
//   Preferences ever mentions a kind by name.
// - Decision #8's confirm/refuse surface (`useIndexProposals`, shared with
//   the status-bar `IndexProposalsDock`) — an app proposing an index of its
//   own folder is confirmed or refused here, not only from the status bar.
//   DELIBERATELY THE SAME HOOK, not a second poll with its own idea of
//   what's pending: the page and the dock are two renderers of one piece of
//   server state, never two disagreeing copies of it.
//
// **What does NOT live here, on purpose:** the file index's own settings —
// roots/ignore patterns, the FDA prompt, the read-only SQL/AI "ask" console
// (Preferences > Indexing, `shell/Indexing.tsx`). That panel predates this
// page, is deeply tested, and is reachable from Preferences by a bookmarked
// URL (`?tab=indexing`) DECISIONS-index-plugins.md's resume pointer warned
// against breaking. Folding its 524 lines in here would either duplicate
// that state (two controls that can disagree about the same ignore list) or
// delete a working, linked surface for no functional gain — this page links
// to it instead of restating it. See DECISIONS-index-plugins.md's "unit 14"
// entry for this call spelled out.
import { useEffect, useState } from "react";
import {
  confirmIndexProposal,
  deleteIndex,
  getIndexKinds,
  indexStatus,
  refuseIndexProposal,
  startIndexScan,
  type IndexStatus,
} from "@platform/lib/api";
import { useIndexProposals } from "./IndexProposalsDock";
import { proposalRows } from "./index-proposals-lib";
import { INDEX_IDLE_POLL_MS, INDEX_POLL_MS } from "@platform/lib/index-status";
import { formatMtimeFull } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import NotificationCard from "@platform/ui/NotificationCard";

// The label a kind's own card wears. "files" and "apps" are named for what
// they hold rather than for their internal registry key, which is otherwise
// meaningless to a reader; a third-party kind not in this table falls back
// to its raw name (below), unadorned rather than mistranslated.
const KIND_LABELS: Record<string, string> = {
  files: "Files",
  apps: "Apps",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

function ProposalsSection() {
  const { pending, refresh } = useIndexProposals();
  const rows = proposalRows(pending);
  const [busy, setBusy] = useState<string | null>(null);

  if (rows.length === 0) return null;

  const onConfirm = async (folder: string) => {
    setBusy(folder);
    try {
      await confirmIndexProposal(folder);
      await refresh();
    } finally {
      setBusy(null);
    }
  };
  const onRefuse = async (folder: string) => {
    setBusy(folder);
    try {
      await refuseIndexProposal(folder);
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Pending proposals</h2>
      <p className="deploy-muted">
        An app is asking to index a folder of its own (SPEC-index-plugins.md decision #8) —
        nothing is granted until you confirm.
      </p>
      {rows.map((row) => (
        <NotificationCard
          key={row.proposal.folder}
          title={row.name}
          titleMode="id"
          titleTooltip={row.proposal.folder}
          status={row.title}
          navAction={{
            label: busy === row.proposal.folder ? "Confirming…" : "Confirm",
            onClick: () => onConfirm(row.proposal.folder),
            disabled: busy === row.proposal.folder,
          }}
          onDismiss={{
            onClick: () => onRefuse(row.proposal.folder),
            disabled: busy === row.proposal.folder,
            title: "Refuse",
            ariaLabel: "Refuse",
          }}
        />
      ))}
    </section>
  );
}

function KindCard({ kind }: { kind: string }) {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ctrl = new AbortController();
    // Self-rescheduling, like `index-status.ts`'s `useIndexStatus`: while a
    // scan is running the card polls fast so "Scanning…" (and the disabled
    // Re-index/Full scan buttons) actually clears when the worker finishes,
    // instead of only ever refetching on mount or right after a button click
    // — which left the card stuck on "Scanning…" until the whole page was
    // remounted (bugbot finding against a7aef9472).
    const tick = () => {
      indexStatus(ctrl.signal, kind === "files" ? undefined : kind).then(
        (s) => {
          if (!alive) return;
          setStatus(s);
          setError(null);
          timer = setTimeout(tick, s.scanning ? INDEX_POLL_MS : INDEX_IDLE_POLL_MS);
        },
        (e: Error) => {
          if (!alive || e.name === "AbortError") return;
          setError(e.message);
          timer = setTimeout(tick, INDEX_IDLE_POLL_MS);
        },
      );
    };
    tick();
    return () => {
      alive = false;
      if (timer !== null) clearTimeout(timer);
      ctrl.abort();
    };
    // `nonce` restarts the chain right after an action, same as Indexing.tsx's
    // own status poll — a scan/delete just fired should be reflected without
    // waiting for the idle interval to come back around.
  }, [kind, nonce]);

  const act = async (what: () => Promise<string>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      setNote(await what());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setNonce((n) => n + 1);
    }
  };

  const scanKind = kind === "files" ? undefined : kind;

  return (
    <section className="prefs-section">
      <h2>{kindLabel(kind)}</h2>
      {!status && !error && <SkeletonLines rows={2} label={`Loading ${kindLabel(kind)} index status`} />}
      {status && (
        <p className="deploy-muted">
          {status.has_index ? (
            <>
              <b>{status.files_indexed.toLocaleString()}</b> row{status.files_indexed === 1 ? "" : "s"} indexed
              {status.last_completed_at
                ? `, last updated ${formatMtimeFull(status.last_completed_at)}`
                : ""}
              .
            </>
          ) : (
            <b>No index yet.</b>
          )}{" "}
          {status.scanning ? "Scanning…" : ""}
        </p>
      )}
      {status && status.error && !status.scanning && (
        <ErrorBanner>The last scan did not finish: {status.error}</ErrorBanner>
      )}
      <div className="prefs-actions">
        <button
          type="button"
          disabled={busy || !!status?.scanning}
          onClick={() =>
            act(async () => {
              await startIndexScan({ kind: scanKind });
              return "Scan started.";
            })
          }
        >
          {status?.scanning ? "Scanning…" : "Re-index"}
        </button>
        <button
          type="button"
          disabled={busy || !!status?.scanning}
          onClick={() =>
            act(async () => {
              await startIndexScan({ kind: scanKind, full: true });
              return "Full rebuild started.";
            })
          }
        >
          Full scan
        </button>
        <button
          type="button"
          className="btn btn-danger"
          disabled={busy}
          onClick={() =>
            act(async () => {
              await deleteIndex(scanKind);
              return "Index deleted.";
            })
          }
        >
          Delete index
        </button>
      </div>
      {kind === "files" && (
        <p className="deploy-muted">
          Roots, skipped folders, and the read-only SQL/ask console for this store are in{" "}
          <a href="/preferences?tab=indexing">Preferences &gt; Indexing</a>.
        </p>
      )}
      {note && <p className="deploy-muted">{note}</p>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </section>
  );
}

export default function IndexManager() {
  const [kinds, setKinds] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getIndexKinds().then(
      (r) => alive && setKinds(r.kinds),
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="prefs-page index-manager-page">
      <h1 className="prefs-title">Index</h1>
      <p className="deploy-muted">
        Every index this app maintains — the file index behind search, the app-name index over
        ~/Fused, and any third-party index a running app has registered — in one place.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!kinds && !error && <SkeletonLines rows={4} label="Loading indexes" />}
      <ProposalsSection />
      {kinds && kinds.map((kind) => <KindCard key={kind} kind={kind} />)}
    </div>
  );
}
