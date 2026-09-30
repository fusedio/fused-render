import { describe, expect, it } from "bun:test";

import {
  EDIT_APPFILE_PARAM,
  editAppFileFromSearch,
  withoutEditAppFile,
} from "./edit-appfile-lib";

const SPICY = "/Users/me/My Apps/a&b #1 100%.fused";

describe("editAppFileFromSearch", () => {
  it("decodes the path the server quoted once", () => {
    const search = "?" + EDIT_APPFILE_PARAM + "=" + encodeURIComponent(SPICY);
    expect(editAppFileFromSearch(search)).toBe(SPICY);
  });

  it("is null without the param, or with an empty one", () => {
    expect(editAppFileFromSearch("")).toBeNull();
    expect(editAppFileFromSearch("?sort=name")).toBeNull();
    expect(editAppFileFromSearch("?" + EDIT_APPFILE_PARAM + "=")).toBeNull();
  });

  it("reads it beside other view params", () => {
    expect(editAppFileFromSearch("?sort=name&" + EDIT_APPFILE_PARAM + "=%2Ftmp%2Fx.fused&q=1"))
      .toBe("/tmp/x.fused");
  });
});

describe("withoutEditAppFile", () => {
  it("strips only the hand-off param and keeps the rest in order", () => {
    expect(withoutEditAppFile("/explorer/view/a/index.html?sort=name&" + EDIT_APPFILE_PARAM + "=x&q=1"))
      .toBe("/explorer/view/a/index.html?sort=name&q=1");
  });

  it("drops the ? when nothing else is left", () => {
    expect(withoutEditAppFile("/?" + EDIT_APPFILE_PARAM + "=%2Ftmp%2Fx.fused")).toBe("/");
  });

  it("leaves a url without a query alone", () => {
    expect(withoutEditAppFile("/explorer/view/a")).toBe("/explorer/view/a");
  });
});
