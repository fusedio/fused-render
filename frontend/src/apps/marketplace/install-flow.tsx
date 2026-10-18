// What pressing a pill DOES, in one place — so a card, the detail hero and an
// installed row can never disagree — plus the install confirmation sheet.
//
// Get on an app that runs Python opens the sheet first ("Before you install":
// what it will do on this machine), once per app: confirming records the slug
// (server-side, best-effort; D1386), and a later Get of the same app — a reinstall
// after an uninstall — goes straight through.
//
// Update and Retry ask only when the installed copy has LOCAL EDITS
// (`installed.modified`): an update lays the new version over the app's own
// files, so an edited file is replaced. Nothing commits a copy first (a
// Marketplace install has no git trail, unlike a `.fused` update), so the
// sheet says plainly that those edits cannot be recovered; files the app
// does not ship (the user's data), `.venv` and `.fused` are kept. "Update
// all" asks the same once, listing every edited app it would touch.
//
// The sheet is requested through a tiny module store and rendered by ONE
// `<InstallSheetHost/>` (mounted by MarketplacePage), the same shape as
// uninstall-app.ts — a pill deep in a card has no good place to hang a modal.
import { useEffect, useRef, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { Modal } from "@platform/ui/modal/Modal";
import { AppIcon, StatusCallout, TrustList } from "@platform/ui/appstore";
import {
  actionFor,
  installApp,
  isActive,
  openInstalledApp,
  type AppEntry,
  type InstallJob,
} from "@platform/lib/marketplace";
import { formatSize } from "@platform/lib/format";
import { ackMarketplacePython, getMarketplaceAcks } from "@platform/lib/api";
import { trustItemsFor } from "./trust";

// The acknowledged slugs live on the server (GET /api/marketplace/acks, POST
// /api/marketplace/ack; `<marketplace state dir>/acks.json`), D1386. They used
// to be localStorage, but the desktop app walks ports 1777..1787 then an
// ephemeral one and each port is a new origin with empty storage, so a relaunch
// on another port asked again about an app already accepted. Reads stay
// synchronous off `acks`, filled by one GET started at module load: the sheet
// only opens on a click, long after that local request has landed. Until it
// lands the set is empty, and the worst case is the sheet asking once more.
const acks = new Set<string>();

/** import-only (D1386): the old localStorage key, a JSON array of slugs. */
const ACK_KEY = "fused-render:marketplace:python-ack";

function hydrateAcks() {
  if (typeof window === "undefined") return;
  // One-shot import: each leftover slug is posted through the normal write
  // path, counts at once (no re-ask while the POSTs travel), and the key goes
  // once every POST settled — a slug the server refuses would refuse forever.
  try {
    const raw = localStorage.getItem(ACK_KEY);
    if (raw !== null) {
      const arr = JSON.parse(raw) as unknown;
      const slugs = Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string" && !!x) : [];
      for (const s of slugs) acks.add(s);
      void Promise.allSettled(slugs.map((s) => ackMarketplacePython(s))).then(() => {
        try { localStorage.removeItem(ACK_KEY); } catch { /* no storage */ }
      });
    }
  } catch {
    /* storage blocked or unreadable: nothing to import */
  }
  getMarketplaceAcks()
    .then((r) => { for (const s of r.slugs || []) acks.add(s); })
    .catch(() => { /* server unreachable: the sheet may ask again */ });
}
hydrateAcks();

function readAcks(): Set<string> {
  return acks;
}

function writeAck(slug: string) {
  acks.add(slug);
  ackMarketplacePython(slug).catch(() => {
    /* best-effort: it holds for this document; worst case the sheet asks again after a relaunch */
  });
}

// -- The sheet request store ---------------------------------------------------

/** What the one sheet is asking about: a first install of a Python app, or
 *  an update (one app, or "Update all") that would replace local edits. */
type SheetRequest = { kind: "install"; app: AppEntry } | { kind: "update"; apps: AppEntry[] };

let pending: SheetRequest | null = null;
const listeners = new Set<(r: SheetRequest | null) => void>();
function setPending(req: SheetRequest | null) {
  pending = req;
  for (const l of listeners) l(req);
}

function start(app: AppEntry) {
  // installApp records the failure on the job itself (the pill turns into
  // Retry with the reason), so nothing more to say here.
  installApp({ slug: app.slug, name: app.name }).catch(() => {});
}

const hasLocalEdits = (app: AppEntry) => !!app.installed?.modified;

/** "v1.0.0 → v1.1.0", or just the version when an update keeps it (the
 *  author changed files without bumping it). */
function versionStep(app: AppEntry): string {
  const have = app.installed?.version;
  return have && have !== app.version ? `v${have} → v${app.version}` : `v${app.version}`;
}

/** The pill's click, for any surface. */
export function runPrimaryAction(app: AppEntry, job: InstallJob | null): void {
  const action = actionFor(app, job);
  switch (action.kind) {
    case "open":
      if (action.path) openInstalledApp(action.path);
      return;
    case "update":
    case "retry":
      if (hasLocalEdits(app)) setPending({ kind: "update", apps: [app] });
      else start(app);
      return;
    case "get":
      if (app.requires_python && !readAcks().has(app.slug)) setPending({ kind: "install", app });
      else start(app);
      return;
    default:
      return;
  }
}

/** "Update all": straight through when nothing was edited locally, else one
 *  confirmation naming the edited apps (the others update either way). */
export function runUpdateAll(apps: AppEntry[], jobs: Record<string, InstallJob>): void {
  const todo = apps.filter((a) => !isActive(jobs[a.slug]));
  if (todo.length === 0) return;
  if (todo.some(hasLocalEdits)) setPending({ kind: "update", apps: todo });
  else todo.forEach(start);
}

export function InstallSheetHost() {
  const [req, setReq] = useState<SheetRequest | null>(pending);
  useEffect(() => {
    listeners.add(setReq);
    setReq(pending);
    return () => {
      listeners.delete(setReq);
      // Leaving the Marketplace with a sheet up drops the request rather than
      // re-opening it on the next visit.
      pending = null;
    };
  }, []);
  if (!req) return null;
  const close = () => setPending(null);
  if (req.kind === "update") return <UpdateSheet apps={req.apps} onClose={close} />;
  return <InstallSheet app={req.app} onClose={close} />;
}

function UpdateSheet({ apps, onClose }: { apps: AppEntry[]; onClose: () => void }) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const edited = apps.filter(hasLocalEdits);
  const single = apps.length === 1 ? apps[0] : null;
  const confirm = () => {
    onClose();
    apps.forEach(start);
  };
  return (
    <Modal
      title={single ? "Update app" : `Update ${apps.length} apps`}
      onClose={onClose}
      initialFocus={confirmRef}
      width={460}
      dialogClassName="mp-sheet"
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button ref={confirmRef} type="button" className="btn btn-primary mp-sheet-install" onClick={confirm}>
            <RefreshCw size={14} strokeWidth={2.2} aria-hidden="true" />
            {single ? "Update anyway" : apps.length === edited.length ? "Update all anyway" : `Update all ${apps.length}`}
          </button>
        </>
      }
    >
      {single ? (
        <div className="mp-sheet-head">
          <AppIcon src={single.icon_url} name={single.name} seed={single.slug} size={56} />
          <div className="mp-sheet-id">
            <div className="mp-sheet-name">{single.name}</div>
            <div className="mp-sheet-meta">{versionStep(single)}</div>
          </div>
        </div>
      ) : (
        <>
          <p className="mp-sheet-lede is-first">
            {edited.length === apps.length
              ? `${edited.length === 2 ? "Both" : "All"} of these apps have been edited on this machine:`
              : edited.length === 1
                ? "One of these apps has been edited on this machine:"
                : `${edited.length} of these apps have been edited on this machine:`}
          </p>
          <ul className="mp-sheet-apps">
            {edited.map((a) => (
              <li key={a.slug}>
                <AppIcon src={a.icon_url} name={a.name} seed={a.slug} size={28} />
                <span className="mp-sheet-app-name">{a.name}</span>
                <span className="mp-sheet-app-v">{versionStep(a)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <StatusCallout
        tone="warn"
        className="mp-sheet-warn"
        title={single ? "You've edited this app's files" : "Their local edits will be replaced"}
      >
        Updating replaces {single ? "the app's" : "each app's"} own files with the new version, so your changes to
        them are lost — they are not saved anywhere first. Files you added, {single ? "its" : "their"} data and Python
        environment are kept.
      </StatusCallout>
    </Modal>
  );
}

function InstallSheet({ app, onClose }: { app: AppEntry; onClose: () => void }) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const confirm = () => {
    writeAck(app.slug);
    onClose();
    start(app);
  };
  return (
    <Modal
      title="Install app"
      onClose={onClose}
      initialFocus={confirmRef}
      width={440}
      dialogClassName="mp-sheet"
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button ref={confirmRef} type="button" className="btn btn-primary mp-sheet-install" onClick={confirm}>
            <Download size={14} strokeWidth={2.2} aria-hidden="true" />
            Install
          </button>
        </>
      }
    >
      <div className="mp-sheet-head">
        <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={56} />
        <div className="mp-sheet-id">
          <div className="mp-sheet-name">{app.name}</div>
          <div className="mp-sheet-meta">
            {app.author.name} · v{app.version}
            {app.size_bytes > 0 && <> · {formatSize(app.size_bytes)}</>}
          </div>
        </div>
      </div>
      <p className="mp-sheet-lede">Before you install, here is what {app.name} does on your machine.</p>
      <TrustList items={trustItemsFor(app)} />
    </Modal>
  );
}
