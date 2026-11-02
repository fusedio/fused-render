// The Tools step's lead sentence. Chrome is named only where Chrome matters:
// the server answers `n/a` on Windows, where bots never run, so the lead must
// not claim Render drives Chrome for them there.
const TAIL = "Nothing here blocks you — each row says what it is for, and you can finish any of it later.";

export function toolsLead(o: { isBot: boolean; showGit: boolean; chromeApplies: boolean; bundleName: string }): string {
  if (o.isBot) {
    return `Bots think with Claude Code running on this machine and drive a real Google Chrome window of their own. ${TAIL}`;
  }
  const git = o.showGit ? ", installs Community apps with git," : "";
  const chrome = o.chromeApplies ? " and drives Google Chrome for bots" : "";
  if (!o.showGit) return `${o.bundleName} hands your brief to Claude Code to build apps${chrome}. ${TAIL}`;
  return `${o.bundleName} hands your brief to Claude Code to build apps${o.chromeApplies ? `${git} and drives Google Chrome for bots` : " and installs Community apps with git"}. ${TAIL}`;
}
