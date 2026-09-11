// Decision 5: is the query in the listing's merged search field actually a
// filesystem address? `listingAddress` (pure, cheap) says WHAT to ask about
// on every render; this hook is the debounced, abortable `statPath` that
// says whether it is real — the same shape as `FilesHome.tsx`'s own address
// resolution for the home page's box, scoped to one folder instead of home.
import { useEffect, useRef, useState } from "react";
import { statPath } from "@platform/lib/api";
import { INSTANT_DEBOUNCE_MS } from "@platform/lib/instant-search";
import { listingAddress } from "@apps/explorer/listing/listing-address";

export type TypedAddress =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "exists"; path: string; is_dir: boolean }
  | { status: "missing" };

export function useTypedPathAddress(
  query: string,
  fsPath: string,
  home: string | undefined,
): TypedAddress {
  const address = listingAddress(query, fsPath, home);
  const [addr, setAddr] = useState<TypedAddress>({ status: "idle" });
  const ctl = useRef<AbortController | null>(null);

  useEffect(() => {
    ctl.current?.abort();
    setAddr({ status: "idle" });
    if (address === null) return;
    const run = () => {
      ctl.current?.abort();
      const c = new AbortController();
      ctl.current = c;
      setAddr({ status: "checking" });
      statPath(address, c.signal).then(
        (st) => {
          if (c.signal.aborted) return;
          setAddr({ status: "exists", path: st.path, is_dir: st.is_dir });
        },
        (err: Error) => {
          if (c.signal.aborted || err.name === "AbortError") return;
          setAddr({ status: "missing" });
        },
      );
    };
    const timer = window.setTimeout(run, INSTANT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [address]);
  useEffect(() => () => ctl.current?.abort(), []);

  return addr;
}
