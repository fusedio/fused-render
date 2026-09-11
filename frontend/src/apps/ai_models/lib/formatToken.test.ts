import { describe, expect, test } from "bun:test";
import { formatToken } from "./formatToken";

describe("formatToken", () => {
  test("gguf format wins regardless of library", () => {
    expect(formatToken({ format: "gguf", library: "llama.cpp" })).toBe("GGUF");
    expect(formatToken({ format: "gguf", library: null })).toBe("GGUF");
  });

  test("falls back to a readable name off library for known libraries", () => {
    expect(formatToken({ format: null, library: "mlx" })).toBe("MLX");
    expect(formatToken({ format: null, library: "safetensors" })).toBe("Safetensors");
    expect(formatToken({ format: null, library: "transformers" })).toBe("Transformers");
    expect(formatToken({ format: null, library: "sentence-transformers" })).toBe(
      "Sentence-Transformers",
    );
    expect(formatToken({ format: null, library: "diffusers" })).toBe("Diffusers");
  });

  test("llama.cpp / llama_cpp library alone still reads as GGUF", () => {
    expect(formatToken({ format: null, library: "llama.cpp" })).toBe("GGUF");
    expect(formatToken({ format: null, library: "llama_cpp" })).toBe("GGUF");
  });

  test("an unrecognised library is capitalised rather than dropped", () => {
    expect(formatToken({ format: null, library: "onnx" })).toBe("Onnx");
  });

  test("null only when both format and library are null", () => {
    expect(formatToken({ format: null, library: null })).toBeNull();
  });
});
