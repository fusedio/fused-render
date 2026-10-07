// A static, data-free miniature of a widget format, drawn with divs and tokens
// only: placeholder bars instead of names, fixed sample numbers. It renders at
// its natural width; callers scale it with CSS `zoom` on a wrapper (transform
// would not shrink the layout box).
import type { WidgetFormat, WidgetSource } from "./layout";

export type PreviewKind =
  | "cards-apps"
  | "cards-playground"
  | "cards-rows"
  | "cards-plain"
  | "icons"
  | "list"
  | "board"
  | "count-tasks"
  | "count-bots"
  | "count-index";

/** Which illustration a (source, format) pair draws. Pure, so it is testable. */
export function previewKind(source: WidgetSource, format: WidgetFormat): PreviewKind {
  switch (format) {
    case "cards":
      if (source === "apps") return "cards-apps";
      if (source === "playground") return "cards-playground";
      if (source === "sessions" || source === "recents") return "cards-rows";
      return "cards-plain";
    case "icons":
      return "icons";
    case "list":
      return "list";
    case "board":
      return "board";
    case "count":
      return source === "bots" ? "count-bots" : source === "index" ? "count-index" : "count-tasks";
  }
}

type BarWidth = "s" | "m" | "l";
const Bar = ({ w = "m" }: { w?: BarWidth }) => <span className={`fp-ln is-${w}`} />;

function CardInner({ kind }: { kind: PreviewKind }) {
  if (kind === "cards-apps") return <span className="fp-sq is-lg" />;
  if (kind === "cards-playground") {
    return (
      <span className="fp-pair">
        <span className="fp-sq" />
        <span className="fp-arrow">→</span>
        <span className="fp-sq" />
      </span>
    );
  }
  if (kind === "cards-rows") {
    return (
      <span className="fp-stack">
        <Bar w="l" />
        <Bar w="m" />
        <Bar w="l" />
      </span>
    );
  }
  return null;
}

const LIST_PILLS = [true, true, true, false];
const LANES: Array<[string, number]> = [
  ["Queued", 1],
  ["In progress", 2],
  ["Needs you", 1],
];

export function FormatPreview({ source, format }: { source: WidgetSource; format: WidgetFormat }) {
  const kind = previewKind(source, format);
  let body;
  if (kind.startsWith("cards")) {
    body = (
      <div className="fp-strip">
        {[0, 1, 2].map((i) => (
          <div key={i} className="fp-card">
            <div className="fp-card-head">
              <Bar w="m" />
            </div>
            <div className="fp-card-well">
              <CardInner kind={kind} />
            </div>
          </div>
        ))}
        <div className="fp-card is-peek" />
      </div>
    );
  } else if (kind === "icons") {
    body = (
      <div className="fp-icons">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="fp-ico">
            <span className="fp-sq is-lg" />
            <Bar w="s" />
          </div>
        ))}
      </div>
    );
  } else if (kind === "list") {
    body = (
      <div className="fp-list">
        {LIST_PILLS.map((pill, i) => (
          <div key={i} className="fp-row">
            <span className="fp-dot" />
            <Bar w="l" />
            {pill ? <span className="fp-pill" /> : null}
          </div>
        ))}
      </div>
    );
  } else if (kind === "board") {
    body = (
      <div className="fp-board">
        {LANES.map(([name, n]) => (
          <div key={name} className="fp-lane">
            <div className="fp-lane-head">
              <Bar w="m" />
              <span>{n}</span>
            </div>
            {Array.from({ length: n }, (_, i) => (
              <div key={i} className="fp-tk">
                <Bar w="l" />
                <Bar w="s" />
              </div>
            ))}
          </div>
        ))}
      </div>
    );
  } else {
    const [num, sub] =
      kind === "count-bots" ? ["3", <>bots</>] : kind === "count-index" ? ["184k", <>files</>] : ["7", <>open · <b>2 need you</b></>];
    body = (
      <div className="fp-count">
        <span className="fp-num">{num}</span>
        <span className="fp-sub">{sub}</span>
      </div>
    );
  }
  return (
    <div className={`fp fp-${kind.split("-")[0]}`} aria-hidden="true">
      {body}
    </div>
  );
}
