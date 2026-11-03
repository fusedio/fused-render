// A context compaction, drawn as a slim divider instead of the summary's own
// screenful-tall user bubble. The summary is one click away, folded by default.
import { memo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@platform/shadcn/ui/collapsible";

import type { CompactTurn } from "../protocol/controller-api";
import { compactLabel } from "../protocol/compact";
import { MarkdownView } from "./MarkdownView";

export const CompactDivider = memo(function CompactDivider({ turn }: { turn: CompactTurn }) {
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="turn compact">
      <div className="compact-rule">
        <span className="compact-label">
          {compactLabel(turn.preTokens, turn.postTokens, turn.trigger)}
          {turn.text.trim() ? (
            <CollapsibleTrigger className="compact-toggle">
              <Chevron size={12} aria-hidden="true" />
              {open ? "Hide summary" : "Show summary"}
            </CollapsibleTrigger>
          ) : null}
        </span>
      </div>
      <CollapsibleContent className="compact-body">
        <MarkdownView text={turn.text} />
      </CollapsibleContent>
    </Collapsible>
  );
});

export default CompactDivider;
