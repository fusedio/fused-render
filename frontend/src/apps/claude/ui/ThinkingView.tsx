// Thinking, unlabelled by content on purpose (T:15591-15605): the point of
// showing it at all is that it HAPPENED and is inspectable, and a reasoning
// trace summarised in the summary line would be a second thing to read for
// every one the user actually wanted to open.
import { memo } from "react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@platform/shadcn/ui/collapsible";
import { cn } from "@platform/lib/utils";

import { useCardOpen } from "./cardPolicy";
import { MarkdownView } from "./MarkdownView";

export interface ThinkingViewProps {
  text: string;
  /** Collapse-policy key (cardPolicy.cardKey) — position-based, since a
   *  thinking segment carries no id. */
  cardKey: string;
}

/** MEMOIZED beside `ToolChip`: a thinking block is prose through the markdown
 *  funnel, and a settled one never changes. */
export const ThinkingView = memo(function ThinkingView({ text, cardKey }: ThinkingViewProps) {
  const [open, toggle] = useCardOpen(cardKey);
  return (
    <Collapsible open={open} onOpenChange={toggle} className={cn("thinking", open && "is-open")}>
      <CollapsibleTrigger className="thinking-summary">Thought for a moment</CollapsibleTrigger>
      <CollapsibleContent className="thinking-body">
        <MarkdownView text={text} />
      </CollapsibleContent>
    </Collapsible>
  );
});

export default ThinkingView;
