import { expect, test } from "bun:test";

import { fitToImage, imageFields, usableBase, type AttachedImage } from "./imageInput";

const photo: AttachedImage = { path: "/Users/me/ai/inputs/webcam.png", name: "webcam.png" };

test("a model the server says can be edited keeps the attachment", () => {
  expect(usableBase(true, photo)).toBe(photo);
});

test("a render-only model gets no attachment, however one got there", () => {
  // The photo rides the URL across a model switch on purpose; this is the rule
  // that stops the render-only model it lands on from offering it anyway.
  expect(usableBase(false, photo)).toBeNull();
});

test("an older server, which sends no acceptsImage at all, is a no", () => {
  expect(usableBase(undefined, photo)).toBeNull();
});

test("a plain render sends the stage's own size and no image", () => {
  expect(imageFields(null, true, null, 480, 272)).toEqual({ width: 480, height: 272 });
});

test("an edit sends the image and the size fitted to that picture", () => {
  // Not 480x272: that would squash a photograph on the way through the edit.
  expect(imageFields(photo, true, { width: 640, height: 480 }, 480, 272)).toEqual({
    image: photo.path,
    width: 640,
    height: 480,
  });
});

test("an edit whose picture has not been measured yet leaves the size off", () => {
  // The server derives it from the file's own header then — slower, since that
  // caps at 1024, but never the wrong shape.
  expect(imageFields(photo, true, null, 480, 272)).toEqual({ image: photo.path });
});

test("an edit whose size was chosen by hand sends that size", () => {
  expect(imageFields(photo, false, null, 640, 480)).toEqual({
    image: photo.path,
    width: 640,
    height: 480,
  });
});

test("fitting keeps the shape, snaps to a multiple of 16 and never upscales", () => {
  // Each side snaps DOWN on its own, so the ratio drifts a little (1.60 here
  // against the original's 1.54) — the same trade the aspect chips make, and
  // invisible next to what a 16-pixel misalignment does to a pipeline.
  expect(fitToImage({ width: 3024, height: 1964 }, 640)).toEqual({ width: 640, height: 400 });
  // Smaller than the cap: edited at its own size, not blown up to it.
  expect(fitToImage({ width: 300, height: 300 }, 640)).toEqual({ width: 288, height: 288 });
  // The 256 floor wins over the shape on an extreme ratio, as it does server-side.
  expect(fitToImage({ width: 4000, height: 200 }, 640)).toEqual({ width: 640, height: 256 });
});
