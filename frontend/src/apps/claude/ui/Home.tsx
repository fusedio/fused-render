// The landing view: the headline, the big composer card and the three lists
// (T:4215-4331, inventory 05 §B). `#chat.home` hides the topbar, the transcript,
// the chat composer, Back and the footnote; this is what stands in their place.
import "../styles/home.css";
import type { SessionRow } from "../protocol/types";
import { HomeCard, type HomeCardProps } from "./HomeCard";
import { Kebab } from "./Kebab";
import { Lists } from "./Lists";

export interface HomeProps extends HomeCardProps {
  recent: SessionRow[] | null;
  artifacts?: readonly unknown[] | null;
  snaps?: readonly unknown[] | null;
  snapsFailed?: boolean;
  /** A recent row on THIS target becomes the current session in place. */
  onOpenSession(sessionId: string): void;
  /** Rows dim and go inert while a comment mode holds the reader (PR3). */
  listsDisabled?: boolean;
  /** The chat's folder, for the (inert) landing kebab — see below. */
  agentDir?: string | null;
}

export function Home({
  recent,
  artifacts,
  snaps,
  snapsFailed,
  onOpenSession,
  listsDisabled,
  agentDir,
  ...cardProps
}: HomeProps) {
  return (
    <div className="c-home">
      {/* THE KEBAB IS IN BOTH VIEWS, and it is inert here. In T it rides the
          `#anntools` strip, which the landing and the transcript both keep
          (T:3842), so the ⋮ is on screen from the first paint — and nothing in
          the menu can act on a chat that does not exist yet. Rendering nothing
          at all instead made the header grow a button on entering a chat. */}
      <div className="c-home-tools">
        <Kebab
          agentDir={agentDir ?? null}
          file={cardProps.file}
          sessionId=""
          running={false}
          disabled
          onErase={() => {}}
        />
      </div>
      {/* One 32px of air at the bottom of the column, one margin per block
          inside it (T:3245-3252). */}
      <div className="c-home-inner">
        <HomeCard {...cardProps} />
        <Lists
          file={cardProps.file}
          recent={recent}
          artifacts={artifacts}
          snaps={snaps}
          snapsFailed={snapsFailed}
          onOpen={onOpenSession}
          onNavigate={cardProps.onNavigate}
          disabled={listsDisabled}
        />
      </div>
    </div>
  );
}
