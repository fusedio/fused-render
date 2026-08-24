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
import { Button } from "@apps/ai_models/ui/button";

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
    if (match[1])
      nodes.push(
        <code key={key} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
          {raw.slice(1, -1)}
        </code>,
      );
    else if (match[2])
      nodes.push(
        <strong key={key} className="font-semibold">
          {inline(raw.slice(2, -2), key)}
        </strong>,
      );
    else if (match[3])
      nodes.push(
        <em key={key} className="italic">
          {inline(raw.slice(1, -1), key)}
        </em>,
      );
    else if (match[4]) {
      const split = raw.indexOf("](");
      nodes.push(
        <a
          key={key}
          className="text-primary underline underline-offset-2"
          href={raw.slice(split + 2, -1)}
          target="_blank"
          rel="noopener noreferrer"
        >
          {raw.slice(1, split)}
        </a>,
      );
    } else {
      nodes.push(
        <a
          key={key}
          className="text-primary underline underline-offset-2"
          href={raw}
          target="_blank"
          rel="noopener noreferrer"
        >
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
    <div className="my-2 overflow-hidden rounded-md border bg-muted/50">
      <div className="flex items-center justify-between border-b px-3 py-1 text-xs text-muted-foreground">
        <span>{lang || "code"}</span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-1.5 text-xs text-muted-foreground"
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
      <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed">{code}</pre>
    </div>
  );
}

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
          <h3 key={key++} className="mt-4 mb-2 text-base font-semibold first:mt-0">
            {content}
          </h3>
        ) : level === 2 ? (
          <h4 key={key++} className="mt-3 mb-1.5 text-sm font-semibold first:mt-0">
            {content}
          </h4>
        ) : (
          <h5 key={key++} className="mt-2 mb-1 text-sm font-medium first:mt-0">
            {content}
          </h5>
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
      blocks.push(
        bullet ? (
          <ul key={key++} className="my-2 list-disc pl-5 first:mt-0 last:mb-0">
            {items}
          </ul>
        ) : (
          <ol key={key++} className="my-2 list-decimal pl-5 first:mt-0 last:mb-0">
            {items}
          </ol>
        ),
      );
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
      <p key={key++} className="my-2 whitespace-pre-wrap first:mt-0 last:mb-0">
        {para.map((one, at) => (
          <Fragment key={at}>
            {at > 0 && "\n"}
            {inline(one, `p${key}-${at}`)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return <div className="text-sm leading-relaxed">{blocks}</div>;
}
