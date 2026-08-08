import { describe, expect, it } from "bun:test";
import type { FsEntry } from "@platform/lib/api";
import { sortEntries } from "@apps/explorer/listing/sorting";

const f = (name: string, size: number, mtime: number): FsEntry =>
  ({ name, is_dir: false, size, mtime }) as FsEntry;
const d = (name: string, mtime = 0): FsEntry =>
  ({ name, is_dir: true, size: null, mtime }) as FsEntry;

const names = (entries: FsEntry[]) => entries.map((e) => e.name);

describe("sortEntries by name", () => {
  const mixed = [f("beta.txt", 10, 1), d("Alpha"), f("alpha.txt", 20, 2), d("zeta")];

  it("groups directories before files", () => {
    expect(names(sortEntries(mixed, "name", "asc"))).toEqual([
      "Alpha",
      "zeta",
      "alpha.txt",
      "beta.txt",
    ]);
  });

  it("keeps dirs first when the order flips", () => {
    const out = names(sortEntries(mixed, "name", "desc"));
    expect(out.slice(0, 2)).toEqual(["zeta", "Alpha"]);
    expect(out.slice(2)).toEqual(["beta.txt", "alpha.txt"]);
  });

  it("keeps dot entries last, outside the dir/file grouping", () => {
    const withDots = [f("b.txt", 1, 1), d(".hidden"), d("Docs"), f(".env", 1, 1)];
    expect(names(sortEntries(withDots, "name", "asc"))).toEqual([
      "Docs",
      "b.txt",
      ".hidden",
      ".env",
    ]);
  });

  it("compares case-insensitively with a stable exact tiebreak", () => {
    const cased = [f("B.txt", 1, 1), f("a.txt", 1, 1), f("A.txt", 1, 1)];
    expect(names(sortEntries(cased, "name", "asc"))).toEqual(["A.txt", "a.txt", "B.txt"]);
  });
});

describe("sortEntries by size and mtime", () => {
  const mixed = [f("big", 900, 5), d("dir"), f("small", 10, 90)];

  it("still groups directories first", () => {
    expect(names(sortEntries(mixed, "size", "asc"))[0]).toBe("dir");
    expect(names(sortEntries(mixed, "mtime", "desc"))[0]).toBe("dir");
  });

  it("orders files by the key", () => {
    expect(names(sortEntries(mixed, "size", "asc")).slice(1)).toEqual(["small", "big"]);
    expect(names(sortEntries(mixed, "mtime", "asc")).slice(1)).toEqual(["big", "small"]);
  });

  it("does not mutate its input", () => {
    const input = [...mixed];
    sortEntries(input, "size", "desc");
    expect(names(input)).toEqual(names(mixed));
  });
});
