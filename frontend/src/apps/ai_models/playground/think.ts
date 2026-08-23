// Where one reply's deliberation ends and its answer begins.
//
// The obvious reading — `<think>…</think>`, then the reply — is wrong for a
// whole class of local models, which emit only the CLOSING tag:
//
//  * the model's chat template PREFILLS the opening tag into the generation
//    prompt (`mlx-community/Macaw-OptiQ-4bit` ends its prompt
//    `<|im_start|>assistant\n<think>`), so that tag was something the model
//    READ, and it never appears in what the model writes;
//  * or the model never learned to write one at all: LFM2.5's own template
//    strips reasoning out of history by splitting on `</think>` ALONE, never
//    asking for an opening tag, which is Liquid saying in the template that
//    `reasoning</think>answer` is the shape it expects back.
//
// Demanding the pair therefore leaked the entire reasoning trace, closing tag
// and all, into the answer body. So: a closing tag with nothing opening it
// means the block was open from the first token. That is the same rule vLLM's
// reasoning parsers apply ("For models that may not generate start token,
// assume the reasoning content is always at the start") and the state
// llama.cpp names `thinking_forced_open`.
//
// The cost of the rule is a reply that discusses these tags in prose: a bare
// `</think>` in an answer reclassifies everything before it as deliberation.
// Paid knowingly — a model talking ABOUT the tag is rare, a model closing one
// it never opened is two of the models on the Models tab.

/** One reply, split: the deliberation (null when there is none), the answer,
 *  and whether the block is still OPEN — mid-stream, everything is thinking. */
export interface SplitReply {
  think: string | null;
  answer: string;
  thinking: boolean;
}

/** Split one reply into the deliberation and the answer. Both spellings:
 *  `<think>` from reasoning-tuned models, `<thinking>` from whatever a
 *  hand-written system prompt asks for — longer tag first, or `<thinking>`
 *  parses as `<think>` plus stray text. */
export function splitThink(text: string): SplitReply {
  for (const tag of ["thinking", "think"]) {
    const openTag = `<${tag}>`;
    const closeTag = `</${tag}>`;
    const open = text.indexOf(openTag);
    // A block the model opened cannot be closed by a tag BEFORE it, so the
    // search starts past the opening tag when there is one — and at 0 when
    // there is not, which is the forced-open case this module exists for.
    const body = open < 0 ? 0 : open + openTag.length;
    const close = text.indexOf(closeTag, body);
    if (open < 0 && close < 0) continue;
    if (close < 0) return { think: text.slice(body), answer: "", thinking: true };
    const think = text.slice(body, close).trim();
    return {
      // An EMPTY closed block is a hybrid model saying it declined to think
      // (`<think></think>`, or a prefilled tag closed immediately). Nothing to
      // disclose, so no disclosure — an empty "Thought process" is furniture.
      think: think || null,
      answer: text.slice(close + closeTag.length).replace(/^\s+/, ""),
      thinking: false,
    };
  }
  return { think: null, answer: text, thinking: false };
}
