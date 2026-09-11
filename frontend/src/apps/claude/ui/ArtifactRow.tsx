// One published page (T:18501-18538, inventory 05 §E "Row anatomy").
//
// [favicon] [title …] [when] [↗], and TWO doors. The row's own subject is the
// LOCAL file — the thing this window can show — and the globe button is the ONE
// place the hosted page opens, which is why its click stops propagating: a
// press on it must not also count as a press on the row.
//
// A new tab either way, never this frame: this window is a chat (often a pane
// of a split), and navigating it away would end the conversation.
import {
  artLabel,
  artLocalPath,
  artOpenHref,
  type Artifact,
} from "../protocol/artifacts";
import { ago } from "./list-rows";

/** The globe (T:18507-18510, verbatim). */
function GlobeIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <path d="M2 12h20" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

export interface ArtifactRowProps {
  artifact: Artifact;
  /** A mode holds the reader on the chat they are in (`annNavLocked`, PR3). */
  disabled?: boolean;
}

export function ArtifactRow({ artifact, disabled }: ArtifactRowProps) {
  const local = artLocalPath(artifact);
  const href = local ? artOpenHref(local) : artifact.remote_url;
  const open = () => {
    if (disabled || !href) return;
    window.open(href, "_blank", "noopener");
  };
  const pressable = !!href;
  return (
    <div
      className="c-art-row"
      {...(pressable
        ? {
            role: "button",
            tabIndex: 0,
            title: local || artifact.remote_url,
            onClick: open,
            onKeyDown: (ev: React.KeyboardEvent<HTMLDivElement>) => {
              if (ev.key !== "Enter" && ev.key !== " ") return;
              ev.preventDefault();
              open();
            },
          }
        : {})}
    >
      {/* Text, not markup: the favicon is author-chosen text from a transcript
          (T:18512-18514). */}
      <span className="c-art-ic" aria-hidden="true">
        {artifact.favicon || "◻"}
      </span>
      <span className="c-art-title">{artLabel(artifact)}</span>
      <span className="c-art-when">
        {ago(artifact.updated_at || artifact.created_at || 0)}
      </span>
      <a
        className="c-art-go"
        href={artifact.remote_url}
        target="_blank"
        rel="noopener noreferrer"
        title="Open the published page"
        aria-label="Open the published page"
        onClick={(ev) => ev.stopPropagation()}
      >
        <GlobeIcon />
      </a>
    </div>
  );
}
