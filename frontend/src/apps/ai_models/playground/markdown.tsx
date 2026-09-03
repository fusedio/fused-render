// A deliberately tiny markdown renderer for model output in the Playground.
//
// Model output is UNTRUSTED text, so everything here builds React elements —
// React escapes the strings — and nothing ever touches innerHTML. The grammar
// is the slice a chat reply actually uses: fenced code (with a copy button),
// inline code, bold, italic, headings, bullet/numbered lists, links, and
// paragraphs. Anything else renders as the text it is, which for a renderer of
// untrusted output is the correct failure mode.
//
// Links open in a new tab with the opener severed; only http(s) hrefs become
// links at all — a `javascript:` URL in model output stays inert text.
import { Fragment, type ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";

function inline(text: string, keyBase: string): ReactNode[] {
  // One pass, one combined pattern: code spans win over emphasis (backticks
  // protect their contents), then bold, italic, and http(s) links.
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  const nodes: ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) nodes.push(text.slice(last, at));
    const key = `${keyBase}-${index++}`;
    const raw = match[0];
    if (match[1]) nodes.push(<code key={key}>{raw.slice(1, -1)}</code>);
    else if (match[2]) nodes.push(<strong key={key}>{inline(raw.slice(2, -2), key)}</strong>);
    else if (match[3]) nodes.push(<em key={key}>{inline(raw.slice(1, -1), key)}</em>);
    else if (match[4]) {
      const split = raw.indexOf("](");
      nodes.push(
        <a key={key} href={raw.slice(split + 2, -1)} target="_blank" rel="noopener noreferrer">
          {raw.slice(1, split)}
        </a>,
      );
    } else {
      nodes.push(
        <a key={key} href={raw} target="_blank" rel="noopener noreferrer">
          {raw}
        </a>,
      );
    }
    last = at + raw.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  return (
    <div className="mb-2.5 overflow-hidden rounded-lg border border-border bg-background">
      <div className="flex items-center justify-between border-b border-border bg-muted/30 px-2.5 py-0.5 text-[11px] text-muted-foreground">
        <span>{lang || "code"}</span>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="h-5 px-1.5 text-[11px] text-muted-foreground"
          onClick={(e) => {
            void navigator.clipboard.writeText(code);
            const button = e.currentTarget;
            button.textContent = "Copied";
            window.setTimeout(() => {
              button.textContent = "Copy";
            }, 1200);
          }}
        >
          Copy
        </Button>
      </div>
      <pre className="m-0 overflow-x-auto px-3 py-2.5 font-mono text-xs leading-relaxed">{code}</pre>
    </div>
  );
}

/** The rendered reply's typography, as utilities on the wrapper: paragraphs
 *  soft-wrapped, headings on the fixed scale, code spans tinted, links
 *  underlined. */
const MD_CLASS = [
  "[&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
  "[&_p]:mb-2.5 [&_p]:whitespace-pre-wrap",
  "[&_h3]:mt-3.5 [&_h3]:mb-1.5 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:leading-snug",
  "[&_h4]:mt-3.5 [&_h4]:mb-1.5 [&_h4]:text-sm [&_h4]:font-semibold [&_h4]:leading-snug",
  "[&_h5]:mt-3.5 [&_h5]:mb-1.5 [&_h5]:text-sm [&_h5]:font-medium [&_h5]:leading-snug",
  "[&_ul]:mb-2.5 [&_ul]:list-disc [&_ul]:pl-5.5 [&_ol]:mb-2.5 [&_ol]:list-decimal [&_ol]:pl-5.5 [&_li]:my-0.5",
  "[&_code]:rounded-sm [&_code]:bg-muted [&_code]:px-1 [&_code]:py-px [&_code]:font-mono [&_code]:text-xs",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_a]:underline [&_a]:underline-offset-2 [&_a]:hover:text-foreground",
].join(" ");

/** Render one model reply. Streaming-safe: an unterminated fence renders as a
 *  code block of what has arrived so far. */
export function renderMarkdown(text: string): ReactNode {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let i = 0;
  let key = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\S*)\s*$/);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++; // past the closing fence (or the end, mid-stream)
      blocks.push(<CodeBlock key={key++} code={body.join("\n")} lang={fence[1]} />);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      const content = inline(heading[2], `h${key}`);
      blocks.push(
        level === 1 ? (
          <h3 key={key++}>{content}</h3>
        ) : level === 2 ? (
          <h4 key={key++}>{content}</h4>
        ) : (
          <h5 key={key++}>{content}</h5>
        ),
      );
      i++;
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+/);
    const numbered = line.match(/^\s*\d+[.)]\s+/);
    if (bullet || numbered) {
      const items: ReactNode[] = [];
      const test = bullet ? /^\s*[-*]\s+/ : /^\s*\d+[.)]\s+/;
      while (i < lines.length && test.test(lines[i])) {
        items.push(<li key={key++}>{inline(lines[i].replace(test, ""), `li${key}`)}</li>);
        i++;
      }
      blocks.push(bullet ? <ul key={key++}>{items}</ul> : <ol key={key++}>{items}</ol>);
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    // A paragraph: consecutive non-empty, non-special lines, soft-wrapped.
    const para: string[] = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```|^#{1,4}\s|^\s*[-*]\s+|^\s*\d+[.)]\s+/.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    blocks.push(
      <p key={key++}>
        {para.map((one, at) => (
          <Fragment key={at}>
            {at > 0 && "\n"}
            {inline(one, `p${key}-${at}`)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return <div className={MD_CLASS}>{blocks}</div>;
}
