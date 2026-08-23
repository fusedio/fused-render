// Which showcase apps the Playground offers UNDER the stage, for the model
// currently selected. A pure rule, its own module for the same reason pick.ts
// is (D425): read and tested without a DOM.
//
// The contract is the community repo's metadata.json, three optional keys:
//
//   `ai_capabilities` — catalog capability ids ("text-generation", …) the app
//       can drive with ANY model of that capability.
//   `ai_models`       — exact wire model ids (repo id or GGUF filename, the
//       catalog's own vocabulary) when the app only works with specific
//       models. Narrower than ai_capabilities and it WINS: a model hit is a
//       recommendation, a miss hides nothing the capability list still earns.
//   `ai_model_param`  — the URL param the app reads its model from. Never
//       literally `model`: the shell's sidebar owns that key, which is why
//       every AI app in the repo namespaces (chatModel, imageModel, asrModel).
//
// An app with AI lists but no param is unreachable by this feature — we could
// open it but not hand it the model, and a card that silently drops the
// model it advertises is worse than no card — so it is skipped.
export type ShowcaseAppMeta = {
  slug: string;
  name?: string;
  description?: string;
  ai_capabilities?: string[];
  ai_models?: string[];
  ai_model_param?: string;
};

export type PlaygroundAppOffer = {
  app: ShowcaseAppMeta;
  /** True when the app named THIS model in `ai_models` — not merely its
   *  capability. The section badges these; a capability match is just listed. */
  recommended: boolean;
};

const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];

/** The offers for one (capability, model) pair, model-matched apps first.
 *
 *  - `ai_models` includes the selected model id → offered, recommended.
 *  - else `ai_capabilities` includes the capability → offered, plain.
 *  - an `ai_models`-only app whose list misses this model is NOT offered:
 *    that is the field's whole point ("this app is specific to these models").
 *  - no usable `ai_model_param`, or the reserved name `model` → skipped.
 *
 *  Ties keep the catalog's own order (alphabetical by slug from the server's
 *  sorted listdir) — a filter and one stable partition, never a resort.
 */
export function matchPlaygroundApps(
  apps: ShowcaseAppMeta[],
  capability: string,
  modelId: string,
): PlaygroundAppOffer[] {
  const byModel: PlaygroundAppOffer[] = [];
  const byCapability: PlaygroundAppOffer[] = [];
  for (const app of apps) {
    const param = typeof app.ai_model_param === "string" ? app.ai_model_param.trim() : "";
    if (!param || param === "model") continue;
    if (strings(app.ai_models).includes(modelId)) {
      byModel.push({ app, recommended: true });
    } else if (strings(app.ai_capabilities).includes(capability)) {
      byCapability.push({ app, recommended: false });
    }
  }
  return [...byModel, ...byCapability];
}
