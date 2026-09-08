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
import { markdownClass, markdownCodeClass, markdownCodeHeadClass } from "@platform/ui/playground";

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
    <div className={markdownCodeClass}>
      <div className={markdownCodeHeadClass}>
        <span>{lang || "code"}</span>
        <button
          type="button"
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
        </button>
      </div>
      <pre>{code}</pre>
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
  return <div className={markdownClass}>{blocks}</div>;
}
