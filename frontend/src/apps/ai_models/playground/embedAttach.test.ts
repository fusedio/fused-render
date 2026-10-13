import { describe, expect, it } from "bun:test";

import {
  acceptedTypes,
  basename,
  displayName,
  linesFromText,
  mediaKindOf,
  mergePaths,
  pathsFromDrop,
  refusal,
} from "./embedAttach";

describe("mediaKindOf reads the extension and nothing else", () => {
  it("sorts the four kinds, case-insensitively, on either separator", () => {
    expect(mediaKindOf("/x/a.PNG")).toBe("picture");
    expect(mediaKindOf("C:\\x\\song.Mp3")).toBe("audio");
    expect(mediaKindOf("/x/clip.mov")).toBe("video");
    expect(mediaKindOf("/x/notes.md")).toBe("text");
    expect(mediaKindOf("/x/report.pdf")).toBe("other");
    expect(mediaKindOf("/x/noext")).toBe("other");
    expect(mediaKindOf("/x/.hidden")).toBe("other");
  });
});

describe("acceptedTypes follows the server's flags", () => {
  it("is pictures only on a SigLIP and all three on the unified encoder", () => {
    expect(acceptedTypes({ audio: false, video: false })).not.toContain("mp3");
    const all = acceptedTypes({ audio: true, video: true });
    expect(all).toContain("mp3");
    expect(all).toContain("mp4");
    expect(all).toContain("heic");
  });
});

describe("refusal names the file and says where the kind works", () => {
  const unified = acceptedTypes({ audio: true, video: true });
  const siglip = acceptedTypes({ audio: false, video: false });
  it("accepts what the model reads", () => {
    expect(refusal("/x/a.wav", unified)).toBeNull();
    expect(refusal("/x/a.png", siglip)).toBeNull();
  });
  it("sends a text file to Lines mode", () => {
    expect(refusal("/x/notes.txt", unified)).toContain("switch to Lines");
  });
  it("names the model that reads sound when this one does not", () => {
    const why = refusal("/x/voice.m4a", siglip);
    expect(why).toContain("voice.m4a");
    expect(why).toContain("EmbeddingGemma 2");
  });
  it("tells a PDF to become text or pictures first", () => {
    expect(refusal("/x/report.pdf", unified)).toContain("converting");
  });
});

function transfer(data: Record<string, string>): DataTransfer {
  return { getData: (type: string) => data[type] ?? "" } as unknown as DataTransfer;
}

describe("pathsFromDrop reads the two path-carrying payloads", () => {
  it("takes the app's own MIME, one path per line", () => {
    expect(pathsFromDrop(transfer({ "application/x-fused-path": "/a.png\n/b.mp3\n" })))
      .toEqual(["/a.png", "/b.mp3"]);
  });
  it("decodes file:// URIs and skips comments and non-file schemes", () => {
    const uris = "# a comment\nfile:///Users/ada/my%20clip.mp4\nhttps://x/y.png\nfile://localhost/tmp/a.wav";
    expect(pathsFromDrop(transfer({ "text/uri-list": uris })))
      .toEqual(["/Users/ada/my clip.mp4", "/tmp/a.wav"]);
  });
  it("drops the authority slash before a Windows drive letter", () => {
    expect(pathsFromDrop(transfer({ "text/uri-list": "file:///C:/Users/ada/a%20b.png\r\n" })))
      .toEqual(["C:/Users/ada/a b.png"]);
  });
  it("is empty for a Finder drop, which carries bytes only", () => {
    expect(pathsFromDrop(transfer({}))).toEqual([]);
  });
});

describe("mergePaths dedupes and caps, counting what did not fit", () => {
  it("keeps order, skips repeats, reports the overflow", () => {
    expect(mergePaths(["/a"], ["/b", "/a", "/c", "/d"], 3)).toEqual({ next: ["/a", "/b", "/c"], over: 1 });
  });
  it("never reports a duplicate as overflow", () => {
    expect(mergePaths(["/a", "/b"], ["/a", "/b"], 2)).toEqual({ next: ["/a", "/b"], over: 0 });
  });
});

describe("linesFromText and basename", () => {
  it("one trimmed non-empty line per entry, either line ending", () => {
    expect(linesFromText("a\r\n\n  b  \nc")).toEqual(["a", "b", "c"]);
  });
  it("basename splits on either separator", () => {
    expect(basename("C:\\photos\\cat.png")).toBe("cat.png");
    expect(basename("/x/y.mp4")).toBe("y.mp4");
  });
});

describe("displayName hides the upload stamp and nothing else", () => {
  it("strips `<ms>-<n>-` off an uploaded copy and leaves a user's own name alone", () => {
    expect(displayName("/c/embed-playground/1791894474847-2-clip.mp4")).toBe("clip.mp4");
    expect(displayName("/Users/ada/2024-01-01-party.png")).toBe("2024-01-01-party.png");
    expect(displayName("/Users/ada/1234-5-notes.wav")).toBe("1234-5-notes.wav");
  });
});
