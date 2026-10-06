// System-clipboard write, shared by every surface that offers a "Copy …" menu
// entry (the explorer's file ops, the preview header, the app cards' context
// menu). Platform-level rather than explorer-level because the app-card menu
// lives outside the explorer app and an app may not import another app.

// Write text to the system clipboard; resolves true on success, false when the
// Clipboard API is missing or the write is denied. Callers decide whether to
// toast (a failure stays silent — the path is still reachable via Reveal).
export async function copyToClipboard(text: string): Promise<boolean> {
  if (!navigator.clipboard) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// Copy text that is still being fetched. THE WRITE MUST START INSIDE THE
// CLICK: WebKit (the desktop app's WKWebView, Safari) only honours a clipboard
// write while the click's user activation is live, and an `await` in the
// handler — a round-trip to the server for the string — spends it, so
// `await fetch(); await writeText()` is rejected with NotAllowedError every
// time ("Could not copy the command", 2026-10-06). Handing `navigator.
// clipboard.write` a ClipboardItem whose text is a *promise* lets the write
// begin synchronously and the browser fill the text in when it arrives;
// WebKit, Chromium and Firefox all accept that shape. Where ClipboardItem is
// missing (an old engine, a test shim) it falls back to awaiting the text and
// using writeText, which is the pre-existing behaviour and works everywhere
// but WebKit.
//
// Resolves true on success, false when the text never arrives, the API is
// missing, or the write is denied — the same contract as copyToClipboard.
export function copyPendingText(text: Promise<string>): Promise<boolean> {
  if (!navigator.clipboard) return Promise.resolve(false);
  const ClipboardItemCtor = (globalThis as { ClipboardItem?: typeof ClipboardItem }).ClipboardItem;
  if (!ClipboardItemCtor || typeof navigator.clipboard.write !== "function") {
    return text.then((s) => copyToClipboard(s), () => false);
  }
  try {
    const blob = text.then((s) => new Blob([s], { type: "text/plain" }));
    const item = new ClipboardItemCtor({ "text/plain": blob });
    return navigator.clipboard.write([item]).then(() => true, () => false);
  } catch {
    return Promise.resolve(false);
  }
}
