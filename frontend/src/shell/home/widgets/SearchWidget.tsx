// The Home file-search box as a widget. The state a live query needs (is a
// search active, the shared index poll) lives in Home so it can take over the
// page; this widget only draws the box, handed those through context.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useIndexStatus } from "@platform/lib/index-status";
import { FilesSearch } from "@apps/explorer/FilesHome";

export interface SearchHost {
  home: string;
  /** `?q=` from the URL at load; only a mounted search widget consumes it. */
  initialQuery: string;
  searching: boolean;
  setSearching: (active: boolean) => void;
  indexScan: ReturnType<typeof useIndexStatus>;
  requestScan: () => void;
}

export const SearchHostContext = createContext<SearchHost | null>(null);

/** Search takes over the page body while a query is live. The index poll only
    runs while the box needs its "indexing…" caveat; `nonce` makes it look again
    immediately when the box starts a scan. */
export function useSearchHost(home: string): SearchHost {
  const [searching, setSearching] = useState(false);
  const [nonce, setNonce] = useState(0);
  const indexScan = useIndexStatus(searching, nonce);
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;
  return { home, initialQuery, searching, setSearching, indexScan, requestScan: () => setNonce((n) => n + 1) };
}

export function SearchWidget({ edit }: { edit: boolean }) {
  const host = useContext(SearchHostContext);
  const setSearching = host?.setSearching;
  // A box that goes away mid-query must not leave Home stuck in takeover.
  useEffect(() => () => setSearching?.(false), [setSearching]);
  if (edit || !host) {
    // Static stand-in while editing: the live box would autofocus and steal
    // typing from the edit controls.
    return (
      <div className="hw-body hw-search-ph" aria-hidden="true">
        <Search size={16} />
        <span>Search files…</span>
      </div>
    );
  }
  return (
    <FilesSearch
      home={host.home}
      initialQuery={host.initialQuery}
      indexScan={host.indexScan}
      onActiveChange={host.setSearching}
      onScanRequested={host.requestScan}
    />
  );
}
