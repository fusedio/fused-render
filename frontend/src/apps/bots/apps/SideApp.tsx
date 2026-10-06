// The side app's markup (OpenBot index.html section.preview: .sabar + #saframe). PreviewPane renders it right under
// its header. Hidden by bots.css until body.hasapp; body.sideapp swaps the browser view for the frame.
import { copyAppState, appOpenUrl } from "./apps";
import { closeSideApp, reloadSideApp, setSideFrame, sideAppBrowser, sideAppShow, useSideApp } from "./side";

export function SideApp() {
  const a = useSideApp();
  return (
    <>
      <div className="sabar" id="sabar">
        <button className="satab" id="satabb" title="Back to the bot's browser" onClick={sideAppBrowser}>Browser</button>
        <button className="satab" id="sataba" title={a?.dir || "The app running beside the chat"} onClick={sideAppShow}>{a?.name || ""}</button>
        <span className="winacts">
          <button id="sareload" title="Reload the app" onClick={reloadSideApp}>Reload</button>
          <button id="sacopy" title="Copy a link that reopens the app exactly as it is now — paste it to any bot" onClick={() => copyAppState(a)}>Copy state</button>
          <a id="saopen" className="btnlink" href={a ? appOpenUrl(a.dir, a.params) : "#"} target="_blank" rel="noopener" title="Open the app in its own tab">Open in tab</a>
          <button id="saclose" title="Close the app" onClick={closeSideApp}>×</button>
        </span>
      </div>
      <iframe id="saframe" ref={setSideFrame} title="App beside the chat" />
    </>
  );
}
