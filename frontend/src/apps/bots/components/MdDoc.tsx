// Markdown as React elements, for the specialist pane (SpecialistPane.tsx, PaneParts.tsx): memory.md read-only, and
// each line of a memory diff. It parses with the bots app's own marked instance (lib/md.ts mdTokens, the parser behind the chat bubbles) and
// builds elements from the tokens, so nothing goes through innerHTML: every piece of text lands as a React text node
// (escaped), raw HTML in the file shows as its source text, links show as their text (the href in a tooltip), images
// as their alt text. Covers what memory.md holds (headings, lists, inline code, bold / italic / strike, links) plus
// paragraphs, quotes, code blocks, rules and tables; any other token shows as its source.
import { Fragment, useMemo, type ReactNode } from "react";
import type { Token, Tokens } from "marked";
import { mdTokens } from "../lib/md";

/** marked's lexer escapes the text of inline `text`, `codespan` and `escape` tokens (&, <, >, ", '); undo that once,
 *  React escapes on output. */
export const unesc = (s: string): string =>
  s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");

function inline(tokens: Token[] | undefined): ReactNode[] {
  return (tokens || []).map((t, i) => <Fragment key={i}>{inlineOne(t)}</Fragment>);
}

function inlineOne(t: Token): ReactNode {
  switch (t.type) {
    case "text": {
      const x = t as Tokens.Text;
      return x.tokens?.length ? inline(x.tokens) : unesc(x.text);
    }
    case "escape": return unesc((t as Tokens.Escape).text);
    case "strong": return <strong>{inline((t as Tokens.Strong).tokens)}</strong>;
    case "em": return <em>{inline((t as Tokens.Em).tokens)}</em>;
    case "del": return <del>{inline((t as Tokens.Del).tokens)}</del>;
    case "codespan": return <code>{unesc((t as Tokens.Codespan).text)}</code>;
    case "br": return <br />;
    case "link": {
      const x = t as Tokens.Link;
      return <span className="md-link" title={x.href}>{inline(x.tokens)}</span>;
    }
    case "image": return unesc((t as Tokens.Image).text);  // the alt text, escaped by the lexer like any other
    default: return t.raw;  // inline html and anything unknown: its source, as text
  }
}

function blocks(tokens: Token[] | undefined): ReactNode[] {
  return (tokens || []).map((t, i) => <Fragment key={i}>{blockOne(t)}</Fragment>);
}

function blockOne(t: Token): ReactNode {
  switch (t.type) {
    case "space": return null;
    case "heading": {
      const x = t as Tokens.Heading;
      const H = (`h${Math.min(Math.max(x.depth, 1), 6)}`) as "h1";
      return <H>{inline(x.tokens)}</H>;
    }
    case "paragraph": return <p>{inline((t as Tokens.Paragraph).tokens)}</p>;
    case "text": {
      const x = t as Tokens.Text;  // a list item's loose text
      return x.tokens?.length ? inline(x.tokens) : unesc(x.text);
    }
    case "list": {
      const x = t as Tokens.List;
      const items = x.items.map((it, i) => (
        <li key={i}>{it.task ? <span className="md-task" aria-hidden="true">{it.checked ? "☑ " : "☐ "}</span> : null}{blocks(it.tokens)}</li>
      ));
      return x.ordered ? <ol start={typeof x.start === "number" && x.start !== 1 ? x.start : undefined}>{items}</ol> : <ul>{items}</ul>;
    }
    case "code": return <pre><code>{(t as Tokens.Code).text}</code></pre>;
    case "blockquote": return <blockquote>{blocks((t as Tokens.Blockquote).tokens)}</blockquote>;
    case "hr": return <hr />;
    case "table": {
      const x = t as Tokens.Table;
      return (
        <div className="tablewrap"><table>
          <thead><tr>{x.header.map((c, i) => <th key={i}>{inline(c.tokens)}</th>)}</tr></thead>
          <tbody>{x.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{inline(c.tokens)}</td>)}</tr>)}</tbody>
        </table></div>
      );
    }
    default: return t.raw ? <p>{t.raw}</p> : null;  // block html, link definitions, anything unknown: the source as text
  }
}

/** `text` (markdown) as elements; nothing for blank text. Wrap it in an element with the `xp-md` class for the look. */
export function MdDoc({ text }: { text: string }) {
  const out = useMemo(() => (text.trim() ? blocks(mdTokens(text)) : null), [text]);
  return <>{out}</>;
}
