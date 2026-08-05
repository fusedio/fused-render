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
