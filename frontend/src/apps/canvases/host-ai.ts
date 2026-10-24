// The AI gateway's model list for the embedded workbench (D1433).
//
// The canvas host tells the frame which models this Mac can run — every
// ENABLED agent in the catalog (`GET /api/agent-backends`, the same store
// every picker reads) times that agent's models — and the frame shows only
// that list in its inline AI edit widget. A request names a row's `id`; the
// host splits it back into the `provider` + `model` pair `/api/ai` takes.
// The separator is `:` because `/` is `/api/ai`'s local-model shape rule
// (a repo id), and the ids never reach the server as `model` anyway.
import type { AgentBackendRow, AgentCatalog } from "@platform/lib/agent-backends";

export interface HostAiModel {
  /** `<backend>:<model>` — what the frame stores and sends back. */
  id: string;
  backend: string;
  model: string;
  label: string;
  /** "Claude" / "Copilot" / "Codex" / "Gemini" — for the picker's row text. */
  backendLabel: string;
  /** The effort levels this model takes; `[]` when the CLI takes none. */
  efforts: string[];
}

export interface HostAiModels {
  models: HostAiModel[];
  /** The row a fresh frame starts on: the default agent's default model,
   *  else the first row; `""` when every agent is off. */
  default: string;
}

export const hostAiModelId = (backend: string, model: string) => `${backend}:${model}`;

function rowsOf(b: AgentBackendRow): HostAiModel[] {
  return b.models.map((m) => ({
    id: hostAiModelId(b.id, m.id),
    backend: b.id,
    model: m.id,
    label: m.label,
    backendLabel: b.label,
    // The server's own rule (`Model.effort_list`): a per-model list when
    // the CLI said one, else the agent's list, none when `reasoning` is off.
    efforts: m.efforts ?? (m.reasoning === false ? [] : b.efforts),
  }));
}

/** Enabled agents only, in the registry's order. */
export function hostAiModels(catalog: AgentCatalog): HostAiModels {
  const models = catalog.backends.filter((b) => b.enabled).flatMap(rowsOf);
  const def = catalog.backends.find((b) => b.id === catalog.default && b.enabled);
  const wanted = def ? hostAiModelId(def.id, def.default_model) : "";
  const fallback = models[0]?.id ?? "";
  return { models, default: models.some((m) => m.id === wanted) ? wanted : fallback };
}

export function findHostAiModel(list: HostAiModels, id: string): HostAiModel | null {
  return list.models.find((m) => m.id === id) ?? null;
}
