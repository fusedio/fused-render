// The status bar's Index-proposals section (SPEC-index-plugins.md decision
// #8, "the app proposes, the user confirms — never silent"): a running app
// declares an index of its own folder (`manifest.py`), and the ONE thing
// that turns that declaration into a granted root is a user clicking Confirm
// here. Nothing in this file, or in `routers/index_manifest.py` underneath
// it, ever imports or runs a third party's module on its own — this panel
// only ever calls `confirm`/`refuse` (platform/lib/api.ts), which flip a
// folder's own state in `manifest.py`'s store; the caller that actually
// consults `manifest.confirmed_folders()` before doing anything with a
// third-party root is elsewhere, exactly as that module's own docstring
// requires.
//
// SAME `.dl-row` SHAPE AS EVERY OTHER STATUS-BAR PANEL (Models, Engines,
// Jobs, repo updates, waiting tasks, LAN pairings) — drawn through
// `platform/ui/NotificationCard.tsx`. `navAction` ("Confirm") is the quieter
// `.q-all` verb, `onDismiss` (✕, "Refuse") is the row's dismiss slot — a
// closer fit than `liveAction`'s red/stop styling, since refusing isn't
// stopping something in flight, it's declining an ask. Both act on the same
// `folder` and simply trigger the next poll.
//
// A PLAIN POLL, NOT A SHARED STORE: unlike Models (`useAiRuntime`, a store
// several surfaces already subscribe to) there is exactly one reader of
// `GET /api/index/proposals` in this app, so a private `setInterval`-style
// poll costs nothing extra and needs no publish/subscribe machinery.
//
// NO CLIENT-SIDE DISMISS STORE (see index-proposals-lib.ts's own docstring):
// refusing is authoritative SERVER state — `manifest.refuse_index` drops the
// folder from both the pending and confirmed lists — so the next poll simply
// stops reporting it. There is no "the server would show this again" case
// for a client-side store to suppress, unlike repo-updates-lib.ts's need
// for one.
//
// SPLIT INTO A PURE VIEW (`IndexProposalsCardView`) AND A STATEFUL WRAPPER
// (`IndexProposalsDock`, default export) — the same split ModelsDock/
// RepoUpdatesDock use, so a test can render the view directly with a fixed
// row list rather than mocking the poll.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmIndexProposal,
  getIndexProposals,
  refuseIndexProposal,
  type IndexProposal,
} from "@platform/lib/api";
import { proposalRows, type ProposalRow } from "./index-proposals-lib";
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";
import NotificationCard from "@platform/ui/NotificationCard";

// Matches the other quick-status chips (Models/repo updates) rather than a
// job's own tighter cadence — a proposal sits until a human notices it, so
// there is no reason to poll faster than that.
const POLL_MS = 6000;

function ProposalRowView({
  row,
  busy,
  onConfirm,
  onRefuse,
}: {
  row: ProposalRow;
  busy: boolean;
  onConfirm: (folder: string) => void;
  onRefuse: (folder: string) => void;
}) {
  return (
    <NotificationCard
      title={row.name}
      titleMode="id"
      titleTooltip={row.proposal.folder}
      status={row.title}
      navAction={{
        label: busy ? "Confirming…" : "Confirm",
        onClick: () => onConfirm(row.proposal.folder),
        disabled: busy,
      }}
      onDismiss={{
        onClick: () => onRefuse(row.proposal.folder),
        disabled: busy,
        title: "Refuse",
        ariaLabel: "Refuse",
      }}
    />
  );
}

/**
 * The pure, props-in half — see `ModelsCardView`'s own doc for why this
 * split exists. Renders nothing at all when there are no pending proposals:
 * unlike Models/Activity, this chip has no "idle" state worth a permanent
 * slot in the bar — decision #8 is a gate that appears only when there is
 * something to gate.
 */
export function IndexProposalsCardView({
  rows,
  busy,
  collapsed,
  onToggle,
  pinned = false,
  hostProps,
  onConfirm,
  onRefuse,
}: {
  rows: ProposalRow[];
  /** Folder currently mid-confirm/refuse, if any — disables that one row's
   *  buttons so a double click cannot fire the call twice. */
  busy: string | null;
  collapsed: boolean;
  onToggle: () => void;
  pinned?: boolean;
  hostProps?: StatusChipState["hostProps"];
  onConfirm: (folder: string) => void;
  onRefuse: (folder: string) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="dl-host" {...hostProps}>
      <StatusChip
        label="Index"
        count={rows.length}
        tone="on"
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? "Show index proposals" : "Hide index proposals"}
        ariaLabel={`${rows.length} app${rows.length === 1 ? "" : "s"} asking to index a folder`}
        onClick={onToggle}
      />
      {!collapsed && (
        <div className="dl-panel">
          <div className="dl-rows">
            {rows.map((row) => (
              <ProposalRowView
                key={row.proposal.folder}
                row={row}
                busy={busy === row.proposal.folder}
                onConfirm={onConfirm}
                onRefuse={onRefuse}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Polls `GET /api/index/proposals` on `POLL_MS`, discarding any response
 *  superseded by a newer request that landed first (the same `generation`
 *  counter `RepoUpdatesDock.tsx`'s own `useRepoUpdates` uses) and leaving the
 *  last snapshot standing on a failed poll rather than clearing the panel. */
function useIndexProposals() {
  const [pending, setPending] = useState<IndexProposal[]>([]);
  const generation = useRef(0);
  const disposed = useRef(false);

  const poll = useCallback(async () => {
    const mine = ++generation.current;
    try {
      const data = await getIndexProposals();
      if (disposed.current || mine !== generation.current) return;
      setPending(data.pending);
    } catch {
      // Leave the last snapshot standing — a transient fetch failure is not
      // "no proposals", it is "no news".
    } finally {
      if (!disposed.current && mine === generation.current) {
        window.setTimeout(poll, POLL_MS);
      }
    }
  }, []);

  useEffect(() => {
    disposed.current = false;
    poll();
    return () => {
      disposed.current = true;
    };
  }, [poll]);

  return { pending, refresh: poll };
}

// NEVER AUTO-OPENS — same rule as every other chip (`useStatusChip`'s own
// `open = pinned || hovered`, no arrival-driven path at all). A proposal
// arriving is announced by the chip appearing and its count, not by the
// panel throwing itself open over whatever the user is doing.
export default function IndexProposalsDock() {
  const { pending, refresh } = useIndexProposals();
  const rows = proposalRows(pending);
  const chip = useStatusChip("index-proposals");
  const [busy, setBusy] = useState<string | null>(null);

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
    <IndexProposalsCardView
      rows={rows}
      busy={busy}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
      onConfirm={onConfirm}
      onRefuse={onRefuse}
    />
  );
}
