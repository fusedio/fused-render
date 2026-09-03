// THE PANEL GLYPH — one icon for every control whose job is "this panel goes
// away / comes back". A frame with one side filled says both which column and
// that it is a panel, without a stateful picture: the same icon draws the
// collapse control and the expand control, as VS Code/Xcode do. `side` is a
// fact about which column the button belongs to (`left` = the global sidebar,
// `right` = the companion column beside a preview), not a style choice.
import { PanelLeft, PanelRight } from "lucide-react";

export default function PanelIcon({ side }: { side: "left" | "right" }) {
  const Icon = side === "left" ? PanelLeft : PanelRight;
  return <Icon size={16} strokeWidth={2} aria-hidden="true" />;
}
