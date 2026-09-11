import { expect, test } from "bun:test";

import { isTopmost, openModalCount, popModal, pushModal } from "./esc-stack";

test("the innermost modal owns the press, and only it", () => {
  const peek = {};
  const dialog = {};
  pushModal(peek);
  expect(isTopmost(peek)).toBe(true);
  // React mounts children after parents, so the nested dialog registers second.
  pushModal(dialog);
  expect(isTopmost(dialog)).toBe(true);
  expect(isTopmost(peek)).toBe(false);
  // …and the outer one takes the NEXT press, once the inner has gone.
  popModal(dialog);
  expect(isTopmost(peek)).toBe(true);
  popModal(peek);
  expect(openModalCount()).toBe(0);
});

test("a lone modal is always the topmost", () => {
  const only = {};
  pushModal(only);
  expect(isTopmost(only)).toBe(true);
  popModal(only);
});

test("an unregistered modal still answers the press", () => {
  // The stack being empty means nothing has claimed a layer, so the chassis
  // behaves exactly as it did before the stack existed.
  expect(isTopmost({})).toBe(true);
});

test("a parent unmounting first removes the right token", () => {
  const peek = {};
  const dialog = {};
  pushModal(peek);
  pushModal(dialog);
  // A caller closing the OUTER dialog while the inner one is still mounted:
  // removal is by identity, so the inner one is still the layer on top.
  popModal(peek);
  expect(isTopmost(dialog)).toBe(true);
  expect(openModalCount()).toBe(1);
  popModal(dialog);
});

test("popping a token twice is harmless", () => {
  const t = {};
  pushModal(t);
  popModal(t);
  popModal(t);
  expect(openModalCount()).toBe(0);
});
