// The Add-mount form's pure decisions. Everything here is a UX judgement that
// was wrong once and is cheap to get wrong again — which remote a pasted link
// should preselect, what to call the folder, and when a setup flow's handoff is
// allowed to move the user's Remote choice.
import { expect, test } from "bun:test";

import {
  mountRootForLink,
  parseStorageUrl,
  pickRemote,
  shouldApplyPreselect,
  suggestMountName,
} from "./links";
import type { RemoteChoice } from "./links";

// -- parseStorageUrl -----------------------------------------------------------

test("scheme URIs parse to a provider and a bucket/prefix path", () => {
  expect(parseStorageUrl("s3://bucket/a/b")).toEqual({ provider: "s3", path: "bucket/a/b" });
  expect(parseStorageUrl("gs://bucket/a")).toEqual({ provider: "gcs", path: "bucket/a" });
  expect(parseStorageUrl("gcs://bucket")).toEqual({ provider: "gcs", path: "bucket" });
});

test("an AWS console bucket link takes its prefix from the query string", () => {
  expect(
    parseStorageUrl("https://us-east-1.console.aws.amazon.com/s3/buckets/my-bucket?prefix=a/b/"),
  ).toEqual({ provider: "s3", path: "my-bucket/a/b/" });
});

test("an unrelated AWS console page is not a storage link", () => {
  expect(parseStorageUrl("https://console.aws.amazon.com/ec2/home?region=us-east-1")).toBeNull();
});

test("a virtual-hosted S3 URL folds the bucket back into the path", () => {
  expect(parseStorageUrl("https://my-bucket.s3.us-west-2.amazonaws.com/data/x.tif")).toEqual({
    provider: "s3",
    path: "my-bucket/data/x.tif",
  });
});

test("non-storage input yields null so the manual fields are left alone", () => {
  expect(parseStorageUrl("")).toBeNull();
  expect(parseStorageUrl("just some text")).toBeNull();
  expect(parseStorageUrl("ftp://host/path")).toBeNull();
});

// -- mountRootForLink ----------------------------------------------------------

test("a deep link to a file mounts the dataset root, not the scene folder", () => {
  expect(mountRootForLink("sentinel-cogs/sentinel-s2-l2a-cogs/32/T/QR/2025/8/TCI.tif")).toBe(
    "sentinel-cogs/sentinel-s2-l2a-cogs",
  );
});

test("a link to a prefix is kept verbatim", () => {
  expect(mountRootForLink("bucket/a/b/")).toBe("bucket/a/b/");
  expect(mountRootForLink("bucket/data.zarr")).toBe("bucket/data.zarr");
});

// -- suggestMountName ----------------------------------------------------------

test("the folder name is the path's last segment", () => {
  expect(suggestMountName("my-bucket/telemetry")).toBe("telemetry");
});

test("a bare bucket names itself", () => {
  expect(suggestMountName("my-sensor-bucket")).toBe("my-sensor-bucket");
});

test("a numeric or date-like tail is joined with its parent, not used alone", () => {
  // The bug: s3://my-sensor-bucket/telemetry/2026 mounted a folder called
  // "2026", which says nothing about what is in it and collides with the next
  // dataset's 2026.
  expect(suggestMountName("my-sensor-bucket/telemetry/2026")).toBe("telemetry-2026");
  expect(suggestMountName("bucket/runs/2026-08-04")).toBe("runs-2026-08-04");
  expect(suggestMountName("bucket/v2/01")).toBe("v2-01");
});

test("a lone numeric segment still yields a name rather than nothing", () => {
  expect(suggestMountName("2026")).toBe("2026");
});

test("a derived name never contains characters add_mount rejects", () => {
  expect(suggestMountName("bucket/.hidden")).toBe("hidden");
  expect(suggestMountName("bucket/  ")).toBe("bucket");
  expect(suggestMountName("")).toBe("");
});

// -- pickRemote ----------------------------------------------------------------

const choice = (over: Partial<RemoteChoice> & { value: string }): RemoteChoice => ({
  label: over.value,
  kind: "other",
  provider: "s3",
  creates: false,
  ...over,
});

test("an existing credentialed remote beats the anonymous public one", () => {
  // The bug: pasting s3://… selected "Public datasets (no credentials)" even
  // with the user's own AWS remote sitting right there, so a private bucket
  // failed with an access error on a remote that could never have read it.
  const value = pickRemote(
    [
      choice({ value: "aws-open:", kind: "public" }),
      choice({ value: "aws:", kind: "detected" }),
      choice({ value: "mine:", kind: "other" }),
    ],
    "s3",
  );
  expect(value).toBe("mine:");
});

test("detected credentials outrank the public remote", () => {
  const value = pickRemote(
    [choice({ value: "aws-open:", kind: "public" }), choice({ value: "aws:", kind: "detected" })],
    "s3",
  );
  expect(value).toBe("aws:");
});

test("an existing remote beats a suggestion that must be created first", () => {
  const value = pickRemote(
    [
      choice({ value: "suggest:aws-profile", kind: "detected", creates: true }),
      choice({ value: "aws-open:", kind: "public" }),
    ],
    "s3",
  );
  expect(value).toBe("aws-open:");
});

test("suggestions are ranked among themselves the same way", () => {
  const value = pickRemote(
    [
      choice({ value: "suggest:gcs-open", kind: "public", creates: true }),
      choice({ value: "suggest:gcloud-adc", kind: "detected", creates: true, provider: "gcs" }),
    ],
    "gcs",
  );
  expect(value).toBe("suggest:gcloud-adc");
});

test("a remote for the other cloud is never picked", () => {
  expect(pickRemote([choice({ value: "aws:", provider: "s3" })], "gcs")).toBeUndefined();
  expect(pickRemote([], "s3")).toBeUndefined();
});

// -- shouldApplyPreselect ------------------------------------------------------

test("nothing pending means nothing to apply", () => {
  expect(shouldApplyPreselect(null, null, ["aws:"])).toBe(false);
});

test("a pending preselect waits for the reload that carries its remote", () => {
  // finishSetup sets the preselect as the modal closes — before getMounts()
  // comes back — so the remote is routinely absent on the first render.
  expect(shouldApplyPreselect("gdrive:", null, ["aws:"])).toBe(false);
  expect(shouldApplyPreselect("gdrive:", null, ["aws:", "gdrive:"])).toBe(true);
});

test("a preselect is applied once and never re-stomps a later manual choice", () => {
  // The mount list re-reads itself (upload poll, refresh-on-return), and every
  // one of those renders used to re-run the preselect and throw away whatever
  // Remote the user had since picked.
  expect(shouldApplyPreselect("gdrive:", "gdrive:", ["gdrive:"])).toBe(false);
});

test("a second setup flow's preselect applies even after a first one did", () => {
  expect(shouldApplyPreselect("box:", "gdrive:", ["gdrive:", "box:"])).toBe(true);
});
