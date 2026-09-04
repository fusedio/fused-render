// The home search's callout for `IndexGap === "fda"`: the packaged mac app has
// no Full Disk Access, so no index scan may start (shell/index_gate.py), so
// there is nothing to search. The one thing the user can do about it is grant
// the access, which is what this offers — in the `.fh-index-cta` slot the
// "Index my files" button uses for the buildable case, because it is the same
// kind of thing: the single action this screen asks for.
//
// Reads the shared FDA store (platform/lib/fda.ts) rather than rendering
// FdaStrip: the strip shows itself only after a REFUSED read, and nothing here
// was refused — nothing was even attempted. Same three faces as the wizard's
// FdaStep: open Settings; the grant landed and a relaunch remains
// (`pending_relaunch`); granted (which this process can only see after that
// relaunch, so in practice the callout is gone by then — the startup scan has
// begun and the gap reads `scanning`).
import { useState } from "react";

import { openFdaSettings } from "@platform/lib/api";
import { FDA_COPY, RELAUNCH_HREF, pokeFda, useFda } from "@platform/lib/fda";

export function IndexFdaCta() {
  const fda = useFda();
  const [opened, setOpened] = useState(false);
  const pending = fda?.pending_relaunch === true;
  const open = () => {
    openFdaSettings()
      .then(() => setOpened(true))
      .catch(() => {})
      .finally(pokeFda);
  };
  return (
    <div className="fh-index-cta" data-testid="fh-index-fda">
      <span className="fh-index-cta-text">
        {pending
          ? "Full Disk Access is granted — relaunch FusedRender to start indexing your files."
          : "Search needs Full Disk Access to index your files — nothing is scanned until you grant it."}
      </span>
      {pending ? (
        <a className="btn btn-secondary fh-index-cta-btn" href={RELAUNCH_HREF}>
          {FDA_COPY.relaunch}
        </a>
      ) : (
        <button type="button" className="btn btn-secondary fh-index-cta-btn" onClick={open}>
          {opened ? FDA_COPY.reopen : FDA_COPY.open}
        </button>
      )}
    </div>
  );
}
