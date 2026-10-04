import type { HubModel } from "@platform/lib/api";

const _FILE_FORMAT_TOKENS: Record<string, string> = {
  safetensors: "Safetensors",
  gguf: "GGUF",
  npz: "NPZ",
  onnx: "ONNX",
  bin: "BIN",
};

/** The library half of the token, or null when the Hub gave nothing to name
 *  (`library` absent) — see `formatToken`'s own docstring for how this
 *  combines with the file-format half. Split out so a `format === "gguf"`
 *  row with no `library` value still resolves to "GGUF" through the SAME
 *  fallback the combiner needs anyway (below), rather than a second,
 *  divergent copy of this switch. */
function _libraryToken(library: string | null): string | null {
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

/** Item 9a's short upper-case format token (fix round 5) went blank on
 *  every embeddings row: the server only sets `HubModel.format` for a GGUF
 *  repo, and round 5's label only recognised `library` "mlx"/"safetensors"
 *  — the live embeddings page is entirely `transformers`/
 *  `sentence-transformers`, so no row anywhere earned a token (fix round 6,
 *  item 2).
 *
 *  Item 3 (two-part token): combines the LIBRARY half (`_libraryToken`, off
 *  `library`/`format`) with the FILE half (off `fileFormat`, the on-disk
 *  format read from `siblings`) as `"<library> · <file>"` when they say two
 *  DIFFERENT things ("MLX · Safetensors", "Transformers · Safetensors") —
 *  the library names the runtime, the file names what is actually on disk,
 *  and a reader benefits from both when they diverge.
 *
 *  Collapses to the ONE token when they'd say the same thing twice
 *  (case-insensitively) — a GGUF repo's library half and file half both
 *  resolve to "GGUF", and "GGUF · GGUF" repeats the one fact rather than
 *  adding a second. Falls back to whichever half exists when the other does
 *  not (`fileFormat` absent -> library token alone; `library` absent ->
 *  "Safetensors"/"NPZ"/etc. alone), and only `null` when NEITHER half
 *  resolves to anything (`format`, `library` and `fileFormat` all empty). */
export function formatToken(
  model: Pick<HubModel, "format" | "library" | "fileFormat">,
): string | null {
  const libraryTok = _libraryToken(model.library) ?? (model.format === "gguf" ? "GGUF" : null);
  const fileTok = model.fileFormat ? _FILE_FORMAT_TOKENS[model.fileFormat] ?? null : null;
  if (!libraryTok) return fileTok;
  if (!fileTok) return libraryTok;
  if (fileTok.toLowerCase() === libraryTok.toLowerCase()) return libraryTok;
  return `${libraryTok} · ${fileTok}`;
}
