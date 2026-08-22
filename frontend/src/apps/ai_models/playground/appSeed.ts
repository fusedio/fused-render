// The /apps composer seed, and the one name a model wears on this tab.
//
// Both moved out of `PlaygroundTab.tsx` with the URL-param helpers (lib/
// params.ts): this is ~60 lines of PROSE ASSEMBLY — what to tell a Claude
// session so it can write a page around the model the user just tried — and
// it is neither picker state nor stage rendering. Keeping it beside them made
// the page file the place every playground concern happened to land.
import { readParam } from "@apps/ai_models/lib/params";
import { type AiCatalogModel } from "@platform/lib/api";

/** The display name everywhere on this tab: the curated nickname, or the label
 *  for a cached entry nobody curated. A fallback read, never a derivation. */
export function modelName(model: AiCatalogModel): string {
  return model.nickname || model.label;
}

/** The seed for the /apps composer: everything the Playground knows about the
 *  moment — which model, what it is good for, the settings the user dialled in
 *  (read off the URL, where every non-default already lives), and the page API
 *  that reaches it (`runtime.js`'s names — the seed is read by an app AUTHOR's
 *  session, and camelCase is that API's vocabulary). Ends mid-sentence on
 *  purpose: the user finishes it with what they actually want built. */
export function buildAppSeed(model: AiCatalogModel, capability: string): string {
  const name = model.nickname || model.label;
  const lines: string[] = [
    `Build a fused app around the local AI model "${name}" (${model.id}) — it runs fully offline on this machine.`,
  ];
  if (model.note) lines.push(`About this model: ${model.note}`);
  const opts = (pairs: [string, string | null][]) =>
    pairs
      .filter((p): p is [string, string] => p[1] !== null && p[1] !== "")
      .map(([k, v]) => `, ${k}: ${v}`)
      .join("");
  if (capability === "text-generation") {
    const extra = opts([
      ["temperature", readParam("temp")],
      ["topP", readParam("topp")],
      ["maxTokens", readParam("maxtok")],
      ["systemPrompt", readParam("system") ? JSON.stringify(readParam("system")) : null],
    ]);
    lines.push(
      "It generates text. Call it from the page with " +
        `fused.ai(prompt, { model: ${JSON.stringify(model.id)}${extra}, history, onChunk }) — ` +
        "it streams tokens through onChunk and resolves with { text, usage }. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "text-to-image") {
    const extra = opts([
      ["width", readParam("w")],
      ["height", readParam("h")],
      ["steps", readParam("steps") ?? (model.defaults?.steps != null ? String(model.defaults.steps) : null)],
      ["guidance", readParam("guidance")],
      ["seed", readParam("seed")],
    ]);
    lines.push(
      "It turns a text prompt into a picture. Call it from the page with " +
        `await fused.ai.image({ prompt, model: ${JSON.stringify(model.id)}${extra}, onProgress }) — ` +
        "it resolves with { url, seed, ... } and url renders straight into an <img>. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "automatic-speech-recognition") {
    const extra = opts([
      ["task", readParam("task") ? JSON.stringify(readParam("task")) : null],
      ["language", readParam("lang") ? JSON.stringify(readParam("lang")) : null],
      ["vad", readParam("vad") === "0" ? "false" : null],
      ["words", readParam("words") === "1" ? "true" : null],
    ]);
    lines.push(
      "It turns speech into text. Call it from the page with " +
        `await fused.ai.transcribe({ path, model: ${JSON.stringify(model.id)}${extra}, onSegment }) — ` +
        "path is an audio/video file on disk, segments stream through onSegment. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "embeddings") {
    lines.push(
      "It turns text or images into vectors that place similar meanings near each other. " +
        `Call it from the page with await fused.ai.embed({ texts, model: ${JSON.stringify(model.id)} }) ` +
        "(or { paths } for image files) — it resolves with { vectors, dim }. Vectors come back " +
        "unit-length, so the dot product of two of them scores how alike their meanings are.",
    );
  }
  // Addressed to the CLAUDE SESSION the composer spawns, not to the user: the
  // `fused-render-ai` skill is the authoritative contract for these calls
  // (streaming shapes, the model_loading retry dance, error types, export
  // rules), and a seed that names the API without pointing at the contract
  // invites the session to improvise it.
  lines.push(
    "",
    "Before writing any AI code, load the `fused-render-ai` skill — it documents the " +
      "fused.ai contract: streaming, model loading and download progress, every error " +
      "type and how a page should respond, and what an exported app may call.",
    "",
    "The app I want: ",
  );
  return lines.join("\n");
}
