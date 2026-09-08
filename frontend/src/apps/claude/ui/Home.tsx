// The landing view: the headline, the big composer card and the three lists
// (T:4215-4331, inventory 05 §B). `#chat.home` hides the topbar, the transcript,
// the chat composer, Back and the footnote; this is what stands in their place.
import "../styles/home.css";
import type { SessionRow } from "../protocol/types";
import { HomeCard, type HomeCardProps } from "./HomeCard";
import { Lists } from "./Lists";
import { useArtifacts } from "./useArtifacts";
import { useSnapshots } from "./useSnapshots";

export interface HomeProps extends HomeCardProps {
  recent: SessionRow[] | null;
  /** A recent row on THIS target becomes the current session in place. */
  onOpenSession(sessionId: string): void;
  /** Rows dim and go inert while a comment mode holds the reader (PR3). */
  listsDisabled?: boolean;
  /** The chat's template folder: the terminal hand-off in the landing kebab,
   *  and the `agent.py` behind the artifacts and snapshots reads below. */
  agentDir?: string | null;
  /** T:19078 `snapInvalidate` — bumped by the chat every time a run ends, so a
   *  turn that edited the file leaves a stale checkpoint chain behind it rather
   *  than a cached one (`useSnapshots`'s third argument). */
  snapInvalidation?: unknown;
}

export function Home({
  recent,
  onOpenSession,
  listsDisabled,
  agentDir,
  snapInvalidation,
  ...cardProps
}: HomeProps) {
  // BOTH READS LIVE HERE, not threaded down from the chat: they are the landing
  // view's own facts, T makes them on every path ONTO this view (`loadArtifacts`
  // / `mountSnapshots` are called from boot AND from Back), and a mount of this
  // component is exactly that path. Nothing in the transcript wants either
  // answer, so nothing above needs to hold them.
  const artifacts = useArtifacts(agentDir ?? null, cardProps.file);
  const snaps = useSnapshots(agentDir ?? null, cardProps.file, snapInvalidation);

  return (
    <div className="c-home">
      {/* NO CONTROL ROW OF ITS OWN (P2-1). The landing used to draw one here
          just to hold the ⋮; T has a single `#anntools` strip above BOTH views
          carrying the preview seats and the menu together (T:3934-4010), and
          `#chat.home #topbar` hides only the identity line on this view — so the
          row is `ClaudeChat`'s and this column starts at the card. The 40px the
          strip occupies is still above us, which is what puts this column where
          :1777 puts it. */}
      {/* The scroller does not swallow the strip above it: that row is chrome
          and must not slide away under the lists. */}
      <div className="c-home-scroll">
        {/* One 32px of air at the bottom of the column, one margin per block
            inside it (T:3245-3252). */}
        <div className="c-home-inner">
          <HomeCard {...cardProps} />
          <Lists
            file={cardProps.file}
            agentDir={agentDir ?? null}
            recent={recent}
            artifacts={artifacts}
            snaps={snaps}
            onOpen={onOpenSession}
            onNavigate={cardProps.onNavigate}
            disabled={listsDisabled}
          />
        </div>
      </div>
    </div>
  );
}
