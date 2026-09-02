// The /apps composer seed, and the one name a model wears on this tab.
//
// Both moved out of `PlaygroundTab.tsx` with the URL-param helpers (lib/
// params.ts): this is ~60 lines of PROSE ASSEMBLY — what to tell a Claude
// session so it can write a page around the model the user just tried — and
// it is neither picker state nor stage rendering. Keeping it beside them made
// the page file the place every playground concern happened to land.
import { readParam } from "@apps/ai_models/lib/params";
import { type AiCatalogModel } from "@platform/lib/api";
import { type AppAnnotation } from "@platform/lib/appAnnotation";

/** The display name everywhere on this tab: the curated nickname, or the label
 *  for a cached entry nobody curated. A fallback read, never a derivation. */
export function modelName(model: AiCatalogModel): string {
  return model.nickname || model.label;
}

/** An annotation the /apps composer shows as a chip (`@LFM2.5`, Cursor-file-tag
 *  style) instead of dumping prose into the prompt box. `detail` carries the
 *  same instructions the old inline seed used to open with — model id, what
 *  it's good for, the settings tuned in the Playground, the page API that
 *  reaches it — spliced into the prompt at submit time, invisible to the user.
 *  `capability` rides along as its own field as well as shaping `detail`: the
 *  composer filters its starter chips by it, and re-deriving it from the prose
 *  is not something the other side can do. */
export function buildAppAnnotation(model: AiCatalogModel, capability: string): AppAnnotation {
  return {
    id: model.id,
    name: modelName(model),
    detail: buildAppSeedDetail(model, capability),
    capability,
  };
}

/** Everything the Playground knows about the moment — which model, what it is
 *  good for, the settings the user dialled in (read off the URL, where every
 *  non-default already lives), and the page API that reaches it (`runtime.js`'s
 *  names — read by an app AUTHOR's session, so camelCase is that API's
 *  vocabulary). Used as an `AppAnnotation`'s `detail`. */
function buildAppSeedDetail(model: AiCatalogModel, capability: string): string {
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
        `fused.ai.text({ prompt, model: ${JSON.stringify(model.id)}${extra}, history, onChunk }) — ` +
        "it streams tokens through onChunk and resolves with { text, usage, response, providerMetadata }. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "text-to-image") {
    const extra = opts([
      ["width", readParam("w")],
      ["height", readParam("h")],
      ["steps", readParam("steps") ?? (model.defaults?.steps != null ? String(model.defaults.steps) : null)],
      ["guidance", readParam("guidance") ?? (model.defaults?.guidance != null ? String(model.defaults.guidance) : null)],
      ["seed", readParam("seed")],
    ]);
    lines.push(
      "It turns a text prompt into a picture. Call it from the page with " +
        `await fused.ai.image({ prompt, model: ${JSON.stringify(model.id)}${extra}, onProgress }) — ` +
        "it resolves with { url, seed, ... } and url renders straight into an <img>. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "text-to-video") {
    const extra = opts([
      ["width", readParam("w")],
      ["height", readParam("h")],
      ["frames", readParam("frames")],
      ["steps", readParam("steps") ?? (model.defaults?.steps != null ? String(model.defaults.steps) : null)],
      ["seed", readParam("seed")],
    ]);
    lines.push(
      "It turns a text prompt into a short video with audio. Call it from the page with " +
        `await fused.ai.video({ prompt, model: ${JSON.stringify(model.id)}${extra}, onProgress }) — ` +
        "it resolves with { url, seed, ... } and url renders straight into a <video controls>. " +
        "Apple Silicon only, with no fallback on other platforms — check fused.ai.models.catalog() " +
        "before offering the feature. " +
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
        `await fused.ai.transcribe({ path, model: ${JSON.stringify(model.id)}${extra}, onChunk }) — ` +
        "path is an audio/video file on disk, segments stream through onChunk. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "embeddings") {
    // **The prose is SPLIT by what this model declares, and the reason is that
    // the route refuses the other half** (SPEC §40). A seeded session that was
    // told about `paths` on a prose encoder writes an image search whose every
    // request comes back 400 naming the model — and it writes it confidently,
    // because the seed is the most authoritative thing in its context. Same for
    // `kind` on a dual encoder. So each parameter is mentioned only where the
    // server says it applies, off the same two flags the Playground's own
    // controls read.
    //
    // The negative half is stated too, not merely omitted: "it has no vision
    // tower" stops a session reaching for `paths` on the strength of the
    // capability's own docs, which describe both halves.
    const takesPaths = model.acceptsPaths === true;
    const scheme = model.promptScheme ?? null;
    const call = `await fused.ai.embed({ texts, model: ${JSON.stringify(model.id)} })`;
    lines.push(
      "It turns text into vectors that place similar meanings near each other. " +
        `Call it from the page with ${call} — it resolves with { vectors, dim }. ` +
        "Vectors come back unit-length, so the dot product of two of them scores " +
        "how alike their meanings are.",
    );
    if (takesPaths) {
      lines.push(
        "This model is a DUAL ENCODER: it also embeds pictures, into the same " +
          "space as the text, so a typed phrase can rank photographs. Pass " +
          "{ paths } instead of { texts } — absolute paths, or relative to the " +
          "page — and compare the two sets of vectors with the same dot product.",
      );
    } else {
      lines.push(
        "This model is a TEXT encoder — it has no vision tower, so { paths } is " +
          "refused with a 400 naming the model. Do not build an image search " +
          "around it.",
      );
    }
    if (scheme) {
      lines.push(
        "It is a RETRIEVAL model and instructs a question differently from a " +
          `passage (its scheme is "${scheme}"). Pass kind: "query" for the thing ` +
          'being searched WITH and kind: "document" for the things being searched ' +
          "THROUGH — embed the corpus once as documents, then each search as a " +
          "query. Leaving it out means \"document\", which is internally " +
          "consistent but measurably worse at retrieval than using both sides.",
      );
    } else {
      lines.push(
        "It has no retrieval prompt convention, so there is no kind parameter to " +
          "pass — sending one is a 400. Embed queries and documents the same way.",
      );
    }
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
  );
  return lines.join("\n");
}
