import type { HubModel } from "@platform/lib/api";

/** Item 9a's short upper-case format token (fix round 5) went blank on
 *  every embeddings row: the server only sets `HubModel.format` for a GGUF
 *  repo, and round 5's label only recognised `library` "mlx"/"safetensors"
 *  — the live embeddings page is entirely `transformers`/
 *  `sentence-transformers`, so no row anywhere earned a token (fix round 6,
 *  item 2).
 *
 *  Falls back to a readable name off `library` for the handful worth
 *  spelling out, then to the library string itself (capitalised) for
 *  anything else the Hub reports — never a guess, just the Hub's own value
 *  made presentable — and only `null` when there is truly nothing to show
 *  (`format` AND `library` both null). `format === "gguf"` still wins first:
 *  it is the more specific, server-derived fact, and a GGUF repo's `library`
 *  slot is `library_name`, whatever the author happened to set it to
 *  (`llama.cpp`/`gguf`/absent), not evidence of a different format. */
export function formatToken(model: Pick<HubModel, "format" | "library">): string | null {
  if (model.format === "gguf") return "GGUF";
  const library = model.library;
  if (!library) return null;
  const lower = library.toLowerCase();
  switch (lower) {
    case "gguf":
    case "llama.cpp":
    case "llama_cpp":
      return "GGUF";
    case "mlx":
      return "MLX";
    case "safetensors":
      return "Safetensors";
    case "transformers":
      return "Transformers";
    case "sentence-transformers":
      return "Sentence-Transformers";
    case "diffusers":
      return "Diffusers";
    default:
      // Capitalise the Hub's own string rather than spell out a name for a
      // library this list has never seen — better an honest guess at case
      // than silence.
      return library.charAt(0).toUpperCase() + library.slice(1);
  }
}
