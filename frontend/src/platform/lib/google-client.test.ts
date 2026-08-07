// Parsing the client_secret_*.json the Google Cloud console hands out, and the
// per-machine store that keeps the console trip a one-time cost.
import { expect, test } from "bun:test";

import { googleConsoleUrls, parseGoogleClientJson } from "./google-client";

const INSTALLED = JSON.stringify({
  installed: {
    client_id: "123-abc.apps.googleusercontent.com",
    project_id: "my-proj",
    client_secret: "GOCSPX-secret",
    redirect_uris: ["http://localhost"],
  },
});

test("a Desktop-app client JSON yields the id and secret", () => {
  expect(parseGoogleClientJson(INSTALLED)).toEqual({
    clientId: "123-abc.apps.googleusercontent.com",
    clientSecret: "GOCSPX-secret",
  });
});

test("a web client JSON is tolerated rather than silently ignored", () => {
  // We ask for Desktop, but downloading the wrong type must not present as an
  // empty form with no explanation.
  const web = JSON.stringify({ web: { client_id: "wid", client_secret: "wsec" } });
  expect(parseGoogleClientJson(web)).toEqual({ clientId: "wid", clientSecret: "wsec" });
});

test("a bare id/secret object works too", () => {
  expect(parseGoogleClientJson('{"client_id":"i","client_secret":"s"}')).toEqual({
    clientId: "i",
    clientSecret: "s",
  });
});

test("surrounding whitespace is trimmed off both values", () => {
  expect(parseGoogleClientJson('{"client_id":" i ","client_secret":"\\ns\\n"}')).toEqual({
    clientId: "i",
    clientSecret: "s",
  });
});

// -- the rejections, which are the point ---------------------------------------

test("a file that is not JSON at all is rejected, not half-accepted", () => {
  expect(parseGoogleClientJson("")).toBeNull();
  expect(parseGoogleClientJson("not json")).toBeNull();
  expect(parseGoogleClientJson("[1,2,3]")).toBeNull();
});

test("a service-account key is rejected", () => {
  // The most likely wrong file to drop: it is valid JSON from the same console
  // and carries a client_id, but no client_secret — so it must NOT half-fill
  // the form and fail later at Google with an opaque error.
  const sa = JSON.stringify({
    type: "service_account",
    client_id: "1234567890",
    private_key: "-----BEGIN PRIVATE KEY-----",
  });
  expect(parseGoogleClientJson(sa)).toBeNull();
});

test("a client with an empty secret is rejected", () => {
  expect(parseGoogleClientJson('{"installed":{"client_id":"i","client_secret":"  "}}')).toBeNull();
});

// -- the console deep links ----------------------------------------------------

test("the console links scope to the project once one is known", () => {
  const urls = googleConsoleUrls("my-proj");
  expect(urls.enableApi).toContain("drive.googleapis.com?project=my-proj");
  expect(urls.consentScreen).toContain("/auth/overview?project=my-proj");
  expect(urls.createClient).toContain("/auth/clients/create?project=my-proj");
  // Creating a project is where the id comes FROM, so it never carries one.
  expect(urls.createProject).toBe("https://console.cloud.google.com/projectcreate");
});

test("no project id yields plain links rather than a dangling query", () => {
  const urls = googleConsoleUrls("   ");
  expect(urls.enableApi).toBe(
    "https://console.cloud.google.com/apis/library/drive.googleapis.com"
  );
  expect(urls.createClient).toBe("https://console.cloud.google.com/auth/clients/create");
});

test("a project id with awkward characters is encoded", () => {
  expect(googleConsoleUrls("a b&c").enableApi).toContain("?project=a%20b%26c");
});
