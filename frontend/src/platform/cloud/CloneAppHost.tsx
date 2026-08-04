// The single mount for the "Open a deployed app" modal (SPEC §35 CL-1). Lives at the shell
// so every trigger reaches the same flow — including the Apps page and Home, which render
// WITHOUT the sidebar, so the entry cannot live there.
import { useEffect, useState } from "react";

import { onCloneAppRequest } from "@platform/cloud/cloneApp";
import { navigateUrl } from "@platform/lib/router";
import CloneModal from "@platform/cloud/CloneModal";

export default function CloneAppHost() {
  // `seq` keys the modal, so a second request while one is open remounts it with the new
  // URL rather than leaving a stale `initialSrc` that the auto-preview has already consumed.
  const [req, setReq] = useState<{ src: string; seq: number } | null>(null);
  useEffect(() => {
    let seq = 0;
    return onCloneAppRequest((src) => {
      seq += 1;
      setReq({ src, seq });
    });
  }, []);

  if (req === null) return null;
  return (
    <CloneModal
      key={req.seq}
      initialSrc={req.src}
      onClose={() => setReq(null)}
      // Navigate to the clone once it lands, so the action ends somewhere useful instead of
      // leaving the user to find the new folder themselves.
      onCloned={(result) => navigateUrl(result.view)}
    />
  );
}
