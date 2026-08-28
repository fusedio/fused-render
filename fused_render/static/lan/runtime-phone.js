/*
 * fused runtime overrides for a PHONE BROWSER (fused_render/lan.py).
 *
 * Served only to the local-network listener's browser clients: LanApp answers
 * GET /static/runtime.js with the stock runtime.js followed by this file. The
 * native Fused Render app is NOT given this file — it gets the stock runtime
 * and installs its own bridge (ios/FusedRender/Resources/runtime-ios.js).
 *
 * A phone browser on plain http has no microphone, no screen capture and no
 * page snapshot worth the name, and the desktop fused.capture.* would record
 * the COMPUTER's screen and mic, which is nonsense from a phone. Earlier this
 * file shimmed all of that with the camera and the photo library; the owner
 * judged those shims useless (2026-08-28) and they are gone. Every capture
 * call now fails fast with one error that says where the feature IS
 * available — the computer, or the Fused Render app on this phone.
 */
(function () {
  const fused = window.fused;
  if (!fused) return;

  fused.device = "phone-web";

  const MESSAGE =
    "fused.capture is not available in a phone browser. " +
    "Use Fused Render on the computer, or the Fused Render app on this phone.";

  function unsupported() {
    const err = new Error(MESSAGE);
    err.type = "capture_unsupported";
    err.device = "phone-web";
    return Promise.reject(err);
  }

  fused.capture = {
    screen: unsupported,
    audio: unsupported,
    screenshot: unsupported,
    attach: unsupported,
    list: () => Promise.resolve([]),
    // The desktop's sources() shape, answered honestly: nothing here can record.
    sources: () =>
      Promise.resolve({
        device: "phone-web",
        native: false,
        displays: [],
        microphones: [],
        video: { available: false, granted: false, reason: MESSAGE },
        audio: { available: false, granted: false, reason: MESSAGE },
        systemAudio: { available: false, reason: MESSAGE },
        screenshot: { available: false, reason: MESSAGE },
      }),
  };

  // The file index is a laptop-wide read the LAN side refuses (lan.py); answer
  // in the runtime's own shape so a page degrades instead of surfacing a 404.
  if (fused.fileIndex) {
    const unavailable = () =>
      Promise.reject(Object.assign(new Error("the file index is not available from a phone"), { type: "index_unavailable" }));
    fused.fileIndex = { search: unavailable, query: unavailable };
  }
})();
