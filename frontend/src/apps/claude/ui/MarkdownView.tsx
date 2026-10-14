// The ONE place model prose becomes markup. Every other component in this
// directory puts tool text in through a text node (T:15150-15153): a tool's
// input and output are raw bytes — a file's contents, a command's stdout, an
// MCP server's answer — and only `text`, `thinking` and a plan are prose.
//
// The funnel itself lives in protocol/markdown.ts (marked → DOMPurify, never
// marked alone). This component is the render site and the code-block pass.
import { memo, useEffect, useMemo, useRef } from "react";

import { enhanceCodeBlocks, renderMd, renderMdInert } from "../protocol/markdown";
import { useFilePreview } from "./filePreview";
import { linkifyEnabled, linkifyPaths, PATH_LINK_CLASS } from "./linkifyPaths";

/** The components permitted to call `dangerouslySetInnerHTML`, by name. The
 *  template pins the same list with a test (D246: `addAssistantTurn`,
 *  `makeTyper.tick`, `buildTextView`, `buildThinkingView`, `buildPlanCard`,
 *  plus the chip's plan body per D248) — here every one of those routes through
 *  this single component, so the list is one entry long and the parity test
 *  asserts nothing else in `ui/` reaches for it. */
export const INNER_HTML_SITES = ["MarkdownView"] as const;

export interface MarkdownViewProps {
  text: string;
  className?: string;
  /** Highlight and add copy buttons after paint. FALSE on the streaming path:
   *  `attachCodeCopy` must never run per frame (T:14998-15055). */
  enhance?: boolean;
  /** FALSE: links become their text and images vanish (`renderMdInert`) — for
   *  a row that is itself one click target, the folded reply. */
  links?: boolean;
  /** The chat's working directory — a shell fence's "run" button types into
   *  the terminal drawer `cd`'d here first, so a relative command lands where
   *  the chat is actually working rather than wherever the drawer happens to
   *  be. Omitted where the caller has no cwd to give (ThinkingView, PlanCard). */
  cwd?: string | null;
}

/**
 * MEMOIZED ON BOTH SIDES, and it is the transcript's whole per-frame cost.
 *
 * `renderMd` is marked.parse + DOMPurify.sanitize: not cheap, and running it in
 * the render body meant every re-render re-parsed EVERY text and thinking
 * segment of EVERY turn. The typer paints at ~25 fps while a reply streams
 * (protocol/typer PAINT_MIN_MS), so a transcript that was O(tail) per frame in
 * the template became O(whole transcript) per frame here — which is precisely
 * what T:15545-15554 went out of its way to avoid. `useMemo` keeps one parse per
 * distinct string; `memo` keeps the frames that changed nothing out entirely.
 */
export const MarkdownView = memo(function MarkdownView({
  text,
  className,
  enhance = true,
  links = true,
  cwd,
}: MarkdownViewProps) {
  const ref = useRef<HTMLDivElement>(null);
  const openPreview = useFilePreview();
  const html = useMemo(() => (links ? renderMd(text) : renderMdInert(text)), [text, links]);
  useEffect(() => {
    // After paint, once per FINAL render: hljs rewrites the `<code>` it
    // highlights, and the copy button reads its `<pre>`'s text before joining
    // the tree, so both need the nodes React has already committed.
    if (enhance && ref.current) enhanceCodeBlocks(ref.current, { cwd });
    // Inline `code` naming a real file becomes a click target for the preview
    // overlay. The inert variant (`links={false}`) never does, and neither
    // does a streaming tail (`enhance` false): a stat per frame is not free.
    const root = ref.current;
    if (!root || !linkifyEnabled({ links, enhance, hasOpener: !!openPreview })) return;
    void linkifyPaths(root, { cwd, live: () => ref.current === root });
  }, [html, enhance, links, cwd, openPreview]);
  const onPathActivate = (target: EventTarget | null) => {
    const el = (target as Element | null)?.closest?.("." + PATH_LINK_CLASS);
    const path = el?.getAttribute("data-path");
    if (!path || !openPreview) return false;
    openPreview(path);
    return true;
  };
  return (
    <div
      ref={ref}
      className={className}
      dangerouslySetInnerHTML={{ __html: html }}
      onClick={(ev) => {
        if (onPathActivate(ev.target)) ev.preventDefault();
      }}
      onKeyDown={(ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        if (onPathActivate(ev.target)) ev.preventDefault();
      }}
    />
  );
});

export default MarkdownView;
