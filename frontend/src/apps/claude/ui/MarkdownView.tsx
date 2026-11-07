// The ONE place model prose becomes markup. Every other component in this
// directory puts tool text in through a text node (T:15150-15153): a tool's
// input and output are raw bytes — a file's contents, a command's stdout, an
// MCP server's answer — and only `text`, `thinking` and a plan are prose.
//
// The funnel itself lives in protocol/markdown.ts (marked → DOMPurify, never
// marked alone). This component is the render site and the code-block pass.
import { memo, useEffect, useMemo, useRef } from "react";

import { useHome } from "@platform/lib/home-path";

import { paneSrcFor } from "../pane/paneUrl";
import { resolveAgainst } from "../protocol/filePaths";
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

/** What a click or Enter opens in the preview: inline code `linkifyPaths`
 *  marked, or a markdown link the renderer rewrote to a local file
 *  (`protocol/markdown.ts` `link`). Both carry the path in `data-path`. */
export const PATH_ACTIVATE_SELECTOR = "." + PATH_LINK_CLASS + ", a[data-path]";

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
  const home = useHome();
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
    void linkifyPaths(root, { cwd, home, live: () => ref.current === root });
  }, [html, enhance, links, cwd, home, openPreview]);
  const onPathActivate = (target: EventTarget | null) => {
    const el = (target as Element | null)?.closest?.(PATH_ACTIVATE_SELECTOR);
    const raw = el?.getAttribute("data-path");
    if (!raw) return false;
    // An inline-code link was only marked where an opener exists (linkifyPaths);
    // a markdown link to a local file is marked at render time and still has to
    // open somewhere without one — the viewer in a new tab, its href.
    const path = resolveAgainst(raw, cwd, home);
    if (openPreview) openPreview(path);
    else if (typeof window !== "undefined") {
      window.open(paneSrcFor({ mode: "_render", path: "" }, path), "_blank", "noopener");
    }
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
