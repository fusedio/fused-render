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

test("a GCS console browser link parses to bucket/prefix", () => {
  expect(parseStorageUrl("https://console.cloud.google.com/storage/browser/my-bucket/a/b")).toEqual(
    { provider: "gcs", path: "my-bucket/a/b" },
  );
});

test("a GCS console object-detail link drops the _details marker", () => {
  // …/browser/_details/<bucket>/<key> is the single-object view. Treating
  // "_details" as the bucket produced a path no remote could ever serve.
  expect(
    parseStorageUrl("https://console.cloud.google.com/storage/browser/_details/my-bucket/x.tif"),
  ).toEqual({ provider: "gcs", path: "my-bucket/x.tif" });
});

test("a GCS console link strips the matrix parameter off the bucket", () => {
  // The console hangs ";tab=objects" on the bucket segment, which is UI state,
  // not part of the bucket name.
  expect(
    parseStorageUrl("https://console.cloud.google.com/storage/browser/my-bucket;tab=objects"),
  ).toEqual({ provider: "gcs", path: "my-bucket" });
  expect(
    parseStorageUrl(
      "https://console.cloud.google.com/storage/browser/my-bucket;tab=objects?project=p",
    ),
  ).toEqual({ provider: "gcs", path: "my-bucket" });
});

test("a GCS console object-detail link strips the matrix parameter off the OBJECT", () => {
  // The regression: ";tab=…" was only ever taken off the bucket segment, but a
  // real address-bar copy of the object view hangs it on the KEY. The suffix
  // survived into `path`, so the mount pointed at a key that does not exist —
  // and, since ".tif;tab=live_object" no longer looks like a file extension,
  // mountRootForLink stopped trimming to the dataset root as well.
  expect(
    parseStorageUrl(
      "https://console.cloud.google.com/storage/browser/_details/my-bucket/a/x.tif;tab=live_object",
    ),
  ).toEqual({ provider: "gcs", path: "my-bucket/a/x.tif" });
  expect(
    parseStorageUrl(
      "https://console.cloud.google.com/storage/browser/_details/my-bucket/a/x.tif;tab=live_object?project=p",
    ),
  ).toEqual({ provider: "gcs", path: "my-bucket/a/x.tif" });
});

test("a semicolon INSIDE an object name is left alone", () => {
  // The reason the strip is anchored on ";tab=<word>" rather than a split(";"):
  // ";" is a legal character in a GCS object name, and cutting at the first one
  // would silently truncate a real key.
  expect(
    parseStorageUrl(
      "https://console.cloud.google.com/storage/browser/_details/my-bucket/odd;name.csv",
    ),
  ).toEqual({ provider: "gcs", path: "my-bucket/odd;name.csv" });
});

test("a GCS console bucket link honors ?prefix=, like the S3 branch", () => {
  expect(
    parseStorageUrl(
      "https://console.cloud.google.com/storage/browser/my-bucket;tab=objects?prefix=data/2026/",
    ),
  ).toEqual({ provider: "gcs", path: "my-bucket/data/2026/" });
});

test("a GCS console page that is not a browser view is not a storage link", () => {
  expect(parseStorageUrl("https://console.cloud.google.com/storage/settings")).toBeNull();
  expect(parseStorageUrl("https://console.cloud.google.com/storage/browser")).toBeNull();
  expect(parseStorageUrl("https://console.cloud.google.com/storage/browser/_details")).toBeNull();
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
//
// The handoff is an EVENT ("this flow just finished"), not a value ("this remote
// is selected"), so it is keyed by a nonce rather than by the remote name. Every
// test below is really about that distinction.

const handoff = (remote: string, nonce: number) => ({ remote, nonce });

test("nothing pending means nothing to apply", () => {
  expect(shouldApplyPreselect(null, null, ["aws:"])).toBe(false);
});

test("a pending handoff waits for the reload that carries its remote", () => {
  // finishSetup fires the handoff as the modal closes — before getMounts()
  // comes back — so the remote is routinely absent on the first render.
  expect(shouldApplyPreselect(handoff("gdrive:", 1), null, ["aws:"])).toBe(false);
  expect(shouldApplyPreselect(handoff("gdrive:", 1), null, ["aws:", "gdrive:"])).toBe(true);
});

test("a handoff is applied once and never re-stomps a later manual choice", () => {
  // The mount list re-reads itself (upload poll, refresh-on-return), and every
  // one of those renders used to re-run the handoff and throw away whatever
  // Remote the user had since picked.
  expect(shouldApplyPreselect(handoff("gdrive:", 1), 1, ["gdrive:"])).toBe(false);
});

test("a second setup flow's handoff applies even after a first one did", () => {
  expect(shouldApplyPreselect(handoff("box:", 2), 1, ["gdrive:", "box:"])).toBe(true);
});

test("the SAME remote handed off twice applies both times", () => {
  // The bug: keyed on the remote name, a repeat was indistinguishable from the
  // one already applied. Use "aws-open:" from Public datasets, mount it, reopen
  // the modal and Use "aws-open:" again — the modal closed onto an untouched
  // form. Same for a Drive re-sign-in with Replace, which is the NORMAL way to
  // recover an expired token.
  expect(shouldApplyPreselect(handoff("aws-open:", 2), 1, ["aws-open:"])).toBe(true);
});
