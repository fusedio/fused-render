// The landing page's Artifacts list, read once per mount of the landing view
// (T:18547 `loadArtifacts`, called on every path ONTO the landing page).
//
// `null` is "the read has not answered" and `[]` is "nothing was published from
// here", which is the common case and the reason the section can be absent
// altogether: no heading, no tab, no empty state, no row saying so.
import { useEffect, useState } from "react";
import { loadArtifacts, type Artifact } from "../protocol/artifacts";

export function useArtifacts(
  agentDir: string | null,
  file: string | null,
): Artifact[] | null {
  const [rows, setRows] = useState<Artifact[] | null>(null);
  useEffect(() => {
    if (!agentDir) {
      setRows(null);
      return;
    }
    let live = true;
    setRows(null);
    void loadArtifacts(agentDir, file).then((out) => {
      if (live) setRows(out);
    });
    return () => {
      live = false;
    };
  }, [agentDir, file]);
  return rows;
}
