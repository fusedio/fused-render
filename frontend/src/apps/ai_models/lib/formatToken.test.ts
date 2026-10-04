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

  test("item 3: library + file format combine when they say different things", () => {
    expect(formatToken({ format: null, library: "mlx", fileFormat: "safetensors" })).toBe(
      "MLX · Safetensors",
    );
    expect(
      formatToken({ format: null, library: "transformers", fileFormat: "safetensors" }),
    ).toBe("Transformers · Safetensors");
  });

  test("item 3: GGUF library + GGUF file collapse to one token, never repeated", () => {
    expect(formatToken({ format: "gguf", library: "llama.cpp", fileFormat: "gguf" })).toBe(
      "GGUF",
    );
    expect(formatToken({ format: "gguf", library: null, fileFormat: "gguf" })).toBe("GGUF");
  });

  test("item 3: file format alone when library is null", () => {
    expect(formatToken({ format: null, library: null, fileFormat: "safetensors" })).toBe(
      "Safetensors",
    );
    expect(formatToken({ format: null, library: null, fileFormat: "npz" })).toBe("NPZ");
    expect(formatToken({ format: null, library: null, fileFormat: "onnx" })).toBe("ONNX");
    expect(formatToken({ format: null, library: null, fileFormat: "bin" })).toBe("BIN");
  });

  test("item 3: library alone when fileFormat is absent/null", () => {
    expect(formatToken({ format: null, library: "mlx", fileFormat: null })).toBe("MLX");
    expect(formatToken({ format: null, library: "mlx" })).toBe("MLX");
  });
});
