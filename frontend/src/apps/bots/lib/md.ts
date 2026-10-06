// Tiny markdown → HTML (text escaped first): **bold**, *italic*, `code`, ``` fences, # headings, -/1. lists, links.
// OpenBot core.js md(), verbatim. The output is rendered with dangerouslySetInnerHTML in exactly one place (the
// thread's bubble body); every input character is escaped before any tag is added, so it is safe for bot text.
import { esc } from "./format";

export function md(src: unknown): string {
  const inline = (s: string): string => {
    const codes: string[] = [];
    s = esc(s).replace(/`([^`\n]+)`/g, (_, c) => `\u0000${codes.push(`<code>${c}</code>`) - 1}\u0000`);
    s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
      .replace(/\*\*([^*\n]+?)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^\w*])\*(\S(?:[^*\n]*?\S)?)\*(?![\w*])/g, "$1<i>$2</i>")
      .replace(/(^|[^\w_])_(\S(?:[^_\n]*?\S)?)_(?![\w_])/g, "$1<i>$2</i>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
  };
  const out: string[] = [];
  let para: string[] = [], list: { tag: "ol" | "ul"; items: string[] } | null = null, fence: string[] | null = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((t) => `<li>${inline(t)}</li>`).join("")}</${list.tag}>`); list = null; } };
  for (const raw of String(src ?? "").replace(/\r/g, "").split("\n")) {
    if (fence) { if (/^\s*```/.test(raw)) { out.push(`<pre><code>${esc(fence.join("\n"))}</code></pre>`); fence = null; } else fence.push(raw); continue; }
    const line = raw.replace(/\s+$/, "");
    let m: RegExpExecArray | null;
    if (/^\s*```/.test(line)) { flushPara(); flushList(); fence = []; }
    else if (!line.trim()) { flushPara(); flushList(); }
    else if ((m = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*$/.exec(line))) { flushPara(); flushList(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
    else if (/^\s{0,3}([-*_])(\s*\1){2,}$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); }
    else if ((m = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(line))) {
      flushPara();
      const tag = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(m[2]);
    }
    else if (list && /^\s{2,}/.test(raw)) list.items[list.items.length - 1] += " " + line.trim();  // wrapped list item
    else if ((m = /^\s{0,3}>\s?(.*)$/.exec(line))) { flushPara(); flushList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else { flushList(); para.push(line); }
  }
  if (fence) out.push(`<pre><code>${esc(fence.join("\n"))}</code></pre>`);
  flushPara(); flushList();
  return out.join("");
}
