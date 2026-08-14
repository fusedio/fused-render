// The user's own Google OAuth client (D219, as rewritten): parsing the JSON the
// Google Cloud console hands out, and remembering it so it is entered ONCE PER
// MACHINE rather than once per remote.
//
// Why this exists at all: Google is retiring rclone's built-in shared client ID
// — rclone was told requests made with it start being charged later in 2026,
// after 90 days' notice — so every Drive user must now create their own OAuth
// client. That is the single most error-prone step in the whole flow, which is
// why the downloaded file is a first-class input here and typing is the
// fallback, not the other way round.

// A client id/secret pair, however it was obtained.
export interface GoogleOAuthClient {
  clientId: string;
  clientSecret: string;
}

// The console's download is `{"installed": {...}}` for a Desktop-app client and
// `{"web": {...}}` for a web one. We ask for Desktop (a loopback redirect is
// what `rclone authorize` serves), but tolerating `web` costs one line and
// turns "you downloaded the wrong client type" from a silent empty form into a
// working sign-in — the server and Google both reject a genuinely unusable
// client far more clearly than we could here.
//
// Returns null for anything that is not a client JSON at all, so the caller can
// say so instead of quietly leaving the fields blank (the failure mode when a
// user drops, say, a service-account key by mistake).
export function parseGoogleClientJson(raw: string): GoogleOAuthClient | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const root = parsed as Record<string, unknown>;
  const section = (root.installed ?? root.web ?? root) as Record<string, unknown>;
  if (typeof section !== "object" || section === null) return null;
  const clientId = typeof section.client_id === "string" ? section.client_id.trim() : "";
  const clientSecret =
    typeof section.client_secret === "string" ? section.client_secret.trim() : "";
  if (!clientId || !clientSecret) return null;
  return { clientId, clientSecret };
}

// Persisted so the console trip is a one-time cost. Best-effort localStorage,
// the same pattern (and the same silence on failure) as lib/viewstate.ts,
// lib/sidebarstate.ts and lib/theme.ts — a browser with storage disabled just
// asks again rather than breaking the flow.
//
// rclone ALSO persists the pair into the remote's own config once a remote
// exists, so an existing Drive remote is an equally legitimate source; this is
// the one that survives deleting the remote and re-creating it, which is the
// case the user actually hits.
const KEY = "fused:google-oauth-client";

export function loadGoogleClient(): GoogleOAuthClient | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<GoogleOAuthClient>;
    // Validated on the way OUT, not just in: a half-written or hand-edited
    // entry must not pre-fill the form with a client that cannot work.
    return parsed.clientId && parsed.clientSecret
      ? { clientId: parsed.clientId, clientSecret: parsed.clientSecret }
      : null;
  } catch {
    return null;
  }
}

export function saveGoogleClient(client: GoogleOAuthClient): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(client));
  } catch {
    /* storage unavailable; the user just re-enters it next time */
  }
}

export function clearGoogleClient(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to do — the caller has already cleared its own state */
  }
}

// The Google Cloud console pages the setup stepper walks the user through.
// `project` is optional because step 1 is where they get one; the later links
// scope to it when known so the user is not re-picking a project each time.
export function googleConsoleUrls(project: string) {
  const p = encodeURIComponent(project.trim());
  const q = p ? `?project=${p}` : "";
  return {
    createProject: "https://console.cloud.google.com/projectcreate",
    enableApi: `https://console.cloud.google.com/apis/library/drive.googleapis.com${q}`,
    consentScreen: `https://console.cloud.google.com/auth/overview${q}`,
    createClient: `https://console.cloud.google.com/auth/clients/create${q}`,
  };
}
