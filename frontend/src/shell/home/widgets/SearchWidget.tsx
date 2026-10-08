// The Home file-search box as a widget. The state a live query needs (is a
// search active, the shared index poll) lives in Home so it can take over the
// page; this widget only draws the box, handed those through context.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useIndexStatus } from "@platform/lib/index-status";
import { FilesSearch } from "@apps/explorer/FilesHome";
import type { WidgetSize } from "../layout";

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

export function SearchWidget({ edit, size }: { edit: boolean; size: WidgetSize }) {
  const host = useContext(SearchHostContext);
  const setSearching = host?.setSearching;
  // A box that goes away mid-query must not leave Home stuck in takeover.
  useEffect(() => () => setSearching?.(false), [setSearching]);
  if (edit || !host) {
    // Static stand-in while editing: the live box would autofocus and steal
    // typing from the edit controls.
    return (
      <div className="hw-body hw-ph hw-search-ph" aria-hidden="true">
        <div className="files-search-wrap">
          <div className="files-search">
            <span className="files-search-icon"><Search size={16} /></span>
            <span className="files-search-input hw-ph-text">
              {size === "1x1" ? "Search files…" : "Search your files — or paste a path like ~/Downloads"}
            </span>
            {size !== "1x1" && (
              <span className="files-hero-cta files-search-allfiles">
                All files
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
              </span>
            )}
          </div>
        </div>
      </div>
    );
  }
  return (
    <FilesSearch
      home={host.home}
      initialQuery={host.initialQuery}
      compact={size === "1x1"}
      indexScan={host.indexScan}
      onActiveChange={host.setSearching}
      onScanRequested={host.requestScan}
    />
  );
}
