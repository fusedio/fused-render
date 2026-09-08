// The landing view: the headline, the big composer card and the three lists
// (T:4215-4331, inventory 05 §B). `#chat.home` hides the topbar, the transcript,
// the chat composer, Back and the footnote; this is what stands in their place.
import "../styles/home.css";
import type { SessionRow } from "../protocol/types";
import { HomeCard, type HomeCardProps } from "./HomeCard";
import { Kebab } from "./Kebab";
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
}

export function Home({
  recent,
  onOpenSession,
  listsDisabled,
  agentDir,
  ...cardProps
}: HomeProps) {
  // BOTH READS LIVE HERE, not threaded down from the chat: they are the landing
  // view's own facts, T makes them on every path ONTO this view (`loadArtifacts`
  // / `mountSnapshots` are called from boot AND from Back), and a mount of this
  // component is exactly that path. Nothing in the transcript wants either
  // answer, so nothing above needs to hold them.
  const artifacts = useArtifacts(agentDir ?? null, cardProps.file);
  const snaps = useSnapshots(agentDir ?? null, cardProps.file);

  return (
    <div className="c-home">
      {/* THE CONTROL ROW, and it is a REAL ROW — T's `#anntools` is a layout row
          above both views, never an overlay (T:3926-3931), and it is also what
          puts this column where :1777 puts it: floating the ⋮ over the page took
          the strip's ~40px out of the top of the column, so the whole landing
          sat that much higher than the template's (Akshil, 2026-09-08, #4).

          The menu is REAL here too, with the one item that can mean anything
          without a session: "New session in terminal" (T:13415). It was drawn
          inert, which is the one thing a control must never be. */}
      <div className="c-home-tools">
        <span className="c-hdr-slack" />
        <Kebab
          agentDir={agentDir ?? null}
          file={cardProps.file}
          sessionId=""
          running={false}
          landing
        />
      </div>
      {/* The scroller is INSIDE the row above, not around it: the strip is
          chrome and must not slide away under the lists. */}
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
