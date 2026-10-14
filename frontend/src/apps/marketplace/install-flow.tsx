// What pressing a pill DOES, in one place — so a card, the detail hero and an
// installed row can never disagree — plus the install confirmation sheet.
//
// Get on an app that runs Python opens the sheet first ("Before you install":
// what it will do on this machine), once per app: confirming records the slug
// (localStorage, best-effort), and a later Get of the same app — a reinstall
// after an uninstall — goes straight through. Update and Retry never ask: the
// reader already said yes to this app once.
//
// The sheet is requested through a tiny module store and rendered by ONE
// `<InstallSheetHost/>` (mounted by MarketplacePage), the same shape as
// uninstall-app.ts — a pill deep in a card has no good place to hang a modal.
import { useEffect, useRef, useState } from "react";
import { Download } from "lucide-react";
import { Modal } from "@platform/ui/modal/Modal";
import { AppIcon, TrustList } from "@platform/ui/appstore";
import {
  actionFor,
  installApp,
  openInstalledApp,
  type AppEntry,
  type InstallJob,
} from "@platform/lib/marketplace";
import { formatSize } from "@platform/lib/format";
import { trustItemsFor } from "./trust";

const ACK_KEY = "fused-render:marketplace:python-ack";

function readAcks(): Set<string> {
  try {
    const raw = localStorage.getItem(ACK_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writeAck(slug: string) {
  try {
    const acks = readAcks();
    acks.add(slug);
    localStorage.setItem(ACK_KEY, JSON.stringify([...acks]));
  } catch {
    /* best-effort: worst case the sheet asks again next time */
  }
}

// -- The sheet request store ---------------------------------------------------

let pending: AppEntry | null = null;
const listeners = new Set<(a: AppEntry | null) => void>();
function setPending(app: AppEntry | null) {
  pending = app;
  for (const l of listeners) l(app);
}

function start(app: AppEntry) {
  // installApp records the failure on the job itself (the pill turns into
  // Retry with the reason), so nothing more to say here.
  installApp({ slug: app.slug, name: app.name }).catch(() => {});
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
      start(app);
      return;
    case "get":
      if (app.requires_python && !readAcks().has(app.slug)) setPending(app);
      else start(app);
      return;
    default:
      return;
  }
}

export function InstallSheetHost() {
  const [app, setApp] = useState<AppEntry | null>(pending);
  useEffect(() => {
    listeners.add(setApp);
    setApp(pending);
    return () => {
      listeners.delete(setApp);
      // Leaving the Marketplace with a sheet up drops the request rather than
      // re-opening it on the next visit.
      pending = null;
    };
  }, []);
  if (!app) return null;
  return <InstallSheet app={app} onClose={() => setPending(null)} />;
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
