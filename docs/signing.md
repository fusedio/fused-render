# Code signing & notarization

`scripts/build_dmg.sh` signs `FusedRender.app` using a **Developer ID identity
from your keychain**, and can **notarize + staple** the DMG. Both are optional:
with no credentials the build ad-hoc signs (local testing only, unchanged from
before). This is the D69 realization of the D35 "future signing hook".

Why bother, beyond Gatekeeper: a Developer ID signature is also what stops the
**repeated Downloads/Desktop/Documents permission prompts**. The executor runs
user code in a fresh `Contents/MacOS/python` subprocess; when the app is only
ad-hoc signed, macOS won't attribute that helper's file access to the app, so
it re-prompts every call. One stable Team ID signing the whole bundle makes the
subprocess part of the same app identity → the prompt appears **once**. (The
built-in table/csv/xlsx readers already avoid this by running in-process — see
DECISIONS D68 — but signing is what covers user scripts too.)

## TL;DR

```bash
# Local test build (no credentials) — ad-hoc, right-click → Open once:
bash scripts/build_dmg.sh

# Signed for distribution (Developer ID cert in your keychain):
bash scripts/build_dmg.sh                       # auto-detects a single cert
FUSED_RENDER_CODESIGN_IDENTITY="Developer ID Application: You (TEAMID)" \
  bash scripts/build_dmg.sh                      # …or name it explicitly

# Signed + notarized + stapled (also needs a stored notary profile):
FUSED_RENDER_NOTARY_PROFILE=FUSED_RENDER_NOTARY bash scripts/build_dmg.sh
```

## Environment variables

| Variable | Purpose |
|---|---|
| `FUSED_RENDER_CODESIGN_IDENTITY` | Signing identity — a `Developer ID Application: NAME (TEAMID)` string **or** the cert's SHA-1 hash. If unset, the script auto-detects a single `Developer ID Application` cert in the keychain; if several exist it stops and asks you to set this; if none, it ad-hoc signs. |
| `FUSED_RENDER_CODESIGN_KEYCHAIN` | Optional keychain to search / sign from, e.g. a dedicated unlocked keychain in CI. Defaults to your keychain search list. Path must not contain spaces. |
| `FUSED_RENDER_NOTARY_PROFILE` | Optional `notarytool` keychain profile name. When set (and Developer-ID signed), the DMG is submitted to Apple, waited on, and stapled. Requires a Developer ID signature — ad-hoc + this is a hard error. |

> The build notarizes and staples the **DMG** (the deliverable), so it opens
> without a Gatekeeper warning offline. The app inside is notarized and launches
> normally. If you also distribute the raw `.app` (outside the DMG) and want it
> stapled for offline first launch, submit an app zip separately and
> `xcrun stapler staple FusedRender.app` before packaging.

## Getting a Developer ID signing identity (high level)

1. **Enroll** in the [Apple Developer Program](https://developer.apple.com/programs/)
   ($99/yr). Note your **Team ID** (Membership details) — the `(TEAMID)` in the
   identity name.
2. **Create a "Developer ID Application" certificate** (this is the cert type
   for distributing outside the App Store):
   - Easiest: **Xcode → Settings → Accounts → your team → Manage Certificates
     → `+` → "Developer ID Application"**. Xcode creates the private key,
     requests the cert, and installs both into your **login keychain**.
   - Or manually: create a CSR with **Keychain Access → Certificate Assistant
     → Request a Certificate from a Certificate Authority**, upload it at
     [developer.apple.com/account → Certificates](https://developer.apple.com/account/resources/certificates/list)
     (type "Developer ID Application"), download the `.cer`, and double-click to
     import. The matching private key must be in the same keychain.
3. **Confirm it's usable for signing:**
   ```bash
   security find-identity -v -p codesigning
   ```
   You want a line like `… "Developer ID Application: Your Name (TEAMID)"`.
   That exact string (or the leading hash) is `FUSED_RENDER_CODESIGN_IDENTITY`.

> A **"Apple Development"** or **"Apple Distribution"** cert is *not* the same
> thing and can't ship a notarized DMG — the script ignores those and only
> auto-detects `Developer ID Application`.

## Storing notary credentials in the keychain (high level)

Notarization authenticates with Apple separately from signing. Store the
credentials once as a keychain profile; the build then references it by name.

**App-specific password** (simplest):
1. At [appleid.apple.com](https://appleid.apple.com) → Sign-In & Security →
   **App-Specific Passwords**, generate one for "fused-render notarization".
2. Store the profile:
   ```bash
   xcrun notarytool store-credentials FUSED_RENDER_NOTARY \
     --apple-id you@example.com --team-id TEAMID --password <app-specific-pw>
   ```

**App Store Connect API key** (better for CI — no personal Apple ID):
1. Create a key at [App Store Connect → Users and Access → Integrations →
   App Store Connect API](https://appstoreconnect.apple.com/access/integrations/api)
   and download the `.p8` (you get the **Key ID** and **Issuer ID** there).
2. Store the profile:
   ```bash
   xcrun notarytool store-credentials FUSED_RENDER_NOTARY \
     --key /path/to/AuthKey_XXXX.p8 --key-id KEYID --issuer ISSUERID
   ```

Then build with `FUSED_RENDER_NOTARY_PROFILE=FUSED_RENDER_NOTARY`.

## What the signing step actually does

- Generates an **entitlements** plist (`build/entitlements.plist`) for the
  hardened runtime: `disable-library-validation` (the bundled CPython loads
  third-party native libs — numpy/pyarrow/duckdb — not signed by your Team),
  `allow-jit` + `allow-unsigned-executable-memory` (Python/numeric libs
  allocate & execute code), and `allow-dyld-environment-variables` (`app.py`
  points the interpreter at its bundled runtime via `PYTHONHOME`).
- Signs **inside-out**: every nested Mach-O (dylibs, `.so`, the bundled
  `python`, framework binaries) is signed with `--options runtime --timestamp`
  *first*, then the `.app` is sealed. `--deep` is deliberately avoided — Apple
  advises against it for distribution and it skips nested executables, which
  notarization then rejects.
- Verifies with `codesign --verify --strict`.

## CI notes

- Import the cert into a **dedicated keychain**, unlock it, and set the key
  partition list so `codesign` won't block on a UI prompt:
  ```bash
  security create-keychain -p "$KC_PW" signing.keychain-db
  security set-keychain-settings -lut 3600 signing.keychain-db
  security unlock-keychain -p "$KC_PW" signing.keychain-db
  security import cert.p12 -k signing.keychain-db -P "$P12_PW" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k "$KC_PW" signing.keychain-db
  security list-keychains -d user -s signing.keychain-db login.keychain-db
  ```
  Then `FUSED_RENDER_CODESIGN_KEYCHAIN=signing.keychain-db` and set
  `FUSED_RENDER_CODESIGN_IDENTITY` explicitly (don't rely on auto-detect in CI).
- Prefer the **API-key** notary profile over an app-specific password.

## Troubleshooting

- **`errSecInternalComponent` / signing hangs** — the keychain is locked or the
  key's ACL blocks `codesign`; run `security unlock-keychain` and the
  `set-key-partition-list` above.
- **Notarization "Invalid" verdict** — fetch the log for the failing item:
  `xcrun notarytool log <submission-id> --keychain-profile FUSED_RENDER_NOTARY`.
  Usual causes: a nested Mach-O missing the hardened runtime or a secure
  timestamp (the inside-out loop covers these), or an entitlement Apple
  disallows for Developer ID.
- **"multiple Developer ID Application identities"** — the build stops on
  purpose; set `FUSED_RENDER_CODESIGN_IDENTITY` to the one you want.
