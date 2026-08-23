import { expect, test } from "bun:test";

import { imageFields, usableBase, type AttachedImage } from "./imageInput";

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
  expect(imageFields(null, true, 480, 272)).toEqual({ width: 480, height: 272 });
});

test("an edit sends the image and leaves the size to the base image", () => {
  // Not 480x272: that would resize a photograph down to a thumbnail on the way
  // through the edit, which is what leaving the pair off avoids.
  expect(imageFields(photo, true, 480, 272)).toEqual({ image: photo.path });
});

test("an edit whose size was chosen by hand sends that size", () => {
  expect(imageFields(photo, false, 640, 480)).toEqual({
    image: photo.path,
    width: 640,
    height: 480,
  });
});
