// The webview that shows the grid, the bots pages and the apps. One persistent
// WKWebsiteDataStore (the default) carries the pairing cookie; the app also
// keeps the cookie's value in the Keychain (PairingSecret.swift) and sets it
// for whatever host it is about to load, so reaching the computer by its
// address instead of its name is still paired.
//
// The user agent carries a marker (`FusedRender-iOS/<version>`) so the
// listener can tell this shell apart; the native bridge (CaptureBridge.swift)
// installs its own `fused.capture` on top of the page's runtime.
//
// The server presents one self-signed certificate; the navigation delegate
// accepts exactly the certificate whose fingerprint the pairing code carried,
// for the computer's own hosts (TLSTrust.swift) — which is what gives pages a
// secure context, and with it the microphone and the camera.
import Network
import SwiftUI
import WebKit
import os

private let webLog = Logger(subsystem: "io.fused.render", category: "web")

/// The little the native chrome needs from the page: where it is, whether it
/// can go back, and two verbs. Owned by ConnectedView, filled in by WebView.
@MainActor
final class WebController: ObservableObject {
    weak var webView: WKWebView?
    @Published var location: URL?
    @Published var canGoBack = false
    @Published var title = ""

    func goBack() { webView?.goBack() }
    func load(_ url: URL) { webView?.load(URLRequest(url: url)) }
}

/// How this shell introduces itself to the computer. Not on `WebView` (a
/// SwiftUI `View`, main-actor by inference), so nonisolated code such as the
/// reachability probe can read it without a warning.
enum ShellIdentity {
    static let userAgentMarker: String = {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        return "FusedRender-iOS/\(v)"
    }()

    /// What the server records as the device's name (`X-Fused-Device-Name` on
    /// the /pair request): the device's own name, which iOS reports as a
    /// plain "iPhone" unless the app is entitled to more — still better than
    /// a user-agent guess.
    @MainActor static var deviceName: String {
        let name = UIDevice.current.name.trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? UIDevice.current.model : name
    }
}

struct WebView: UIViewRepresentable {
    let server: Server
    /// The host to reach the computer on right now: its name, or the fallback
    /// address Reachability found it on.
    let host: String
    @Binding var pairURL: URL?
    let controller: WebController
    /// Fired with the host when the grid finishes loading — AppModel treats it
    /// as proof a pending pairing landed (cookie set, token spent).
    var onGridLoaded: (String) -> Void = { _ in }
    /// A main-frame navigation died on the network: ConnectedView runs the
    /// reachability diagnosis and shows why.
    var onNavigationFailed: () -> Void = {}

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        // The marker rides on WebKit's own UA from the very first request (the
        // pairing one included).
        config.applicationNameForUserAgent = "Mobile/15E148 " + Self.userAgentMarker
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        // The native capture bridge: JS side injected at document end (after
        // the page's runtime.js has built window.fused), Swift side receives
        // the `fusedCapture` messages. See CaptureBridge.swift.
        let bridge = context.coordinator.bridge
        config.userContentController.add(bridge, name: CaptureBridge.handlerName)
        if let url = Bundle.main.url(forResource: "runtime-ios", withExtension: "js"),
           let source = try? String(contentsOf: url, encoding: .utf8) {
            config.userContentController.addUserScript(
                WKUserScript(source: source, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        }
        let web = WKWebView(frame: .zero, configuration: config)
        bridge.webView = web
        bridge.server = server
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = true
        web.scrollView.contentInsetAdjustmentBehavior = .never
        // Pull down to reload — the grid (fresh listing) and any app page alike.
        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pulled(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        web.isOpaque = false
        web.backgroundColor = .systemBackground
        context.coordinator.webView = web
        controller.webView = web
        // The page's <title> lands after didFinish for most apps; watch it.
        context.coordinator.titleObservation = web.observe(\.title, options: [.new]) { [controller] web, _ in
            Task { @MainActor in controller.title = web.title ?? "" }
        }
        // The lan pages may route `/` ⇄ `/bots` ⇄ `/bots/<id>` in-page
        // (pushState: no didFinish), so the top bar follows the URL itself.
        // (Only the chrome: onGridLoaded stays on didFinish below.)
        context.coordinator.urlObservation = web.observe(\.url, options: [.new]) { [controller] web, _ in
            Task { @MainActor in
                if let url = web.url { controller.location = url }
            }
        }
        // Record a pairing URL as already-loaded here: updateUIView runs right
        // after and would otherwise load the same /pair again — the token is
        // single-use, so the second hit would land on "code expired".
        context.coordinator.lastPending = pairURL
        context.coordinator.host = host
        context.coordinator.load(pairURL ?? server.url(host: host), pairing: pairURL != nil)
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        context.coordinator.server = server
        context.coordinator.bridge.server = server
        context.coordinator.onNavigationFailed = onNavigationFailed
        let base = server.url(host: host)
        if let pair = pairURL {
            // A fresh pairing request: load it once, then clear so a SwiftUI
            // re-render does not replay it. Guarded against the same URL being
            // seen twice before the async clear lands.
            if context.coordinator.lastPending != pair {
                context.coordinator.lastPending = pair
                // The pending page IS this server's page (a deep link, or the
                // pairing): mark the base as current too, or the next pass
                // would see a stale baseURL and wipe the page with the grid.
                context.coordinator.baseURL = base
                context.coordinator.host = host
                webLog.info("load pending \(pair.absoluteString, privacy: .public)")
                context.coordinator.load(pair, pairing: pair.path == "/pair")
            }
            DispatchQueue.main.async { pairURL = nil }
        } else if context.coordinator.baseURL != base {
            context.coordinator.baseURL = base
            context.coordinator.host = host
            webLog.info("load base \(base.absoluteString, privacy: .public)")
            context.coordinator.load(base, pairing: false)
        }
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(server: server, host: host, controller: controller, onGridLoaded: onGridLoaded,
                    onNavigationFailed: onNavigationFailed)
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var server: Server
        var host: String
        var baseURL: URL
        var lastPending: URL?
        var titleObservation: NSKeyValueObservation?
        var urlObservation: NSKeyValueObservation?
        weak var webView: WKWebView?
        let controller: WebController
        let onGridLoaded: (String) -> Void
        var onNavigationFailed: () -> Void
        let bridge = CaptureBridge()

        /// Set when a navigation failed, cleared when one lands. What makes
        /// the network watch below reload exactly once after a failure rather
        /// than on every Wi-Fi ⇄ cellular handoff of a page that is fine.
        private var navigationFailed = false
        /// The main-frame URL the last navigation asked for. What a retry
        /// reloads when the failed load never committed (`webView.url` is nil
        /// then): during pairing that is `/pair?t=…`, and falling back to the
        /// base URL instead would land on the how-to-pair page, which
        /// didFinish would then take for a pairing that succeeded.
        private var requestedURL: URL?
        private let network = NWPathMonitor()

        init(server: Server, host: String, controller: WebController, onGridLoaded: @escaping (String) -> Void,
             onNavigationFailed: @escaping () -> Void) {
            self.server = server
            self.host = host
            self.baseURL = server.url(host: host)
            self.controller = controller
            self.onGridLoaded = onGridLoaded
            self.onNavigationFailed = onNavigationFailed
            super.init()
            // Coming back onto a network reloads a page that died on the way
            // out. The computer's name follows its address on its own (the
            // OS advertises it); this is the phone's half: without it the
            // webview sits on WebKit's error page until someone pulls to
            // refresh.
            network.pathUpdateHandler = { [weak self] path in
                guard path.status == .satisfied else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { self?.retryAfterNetworkChange() }
            }
            network.start(queue: .main)
        }

        deinit { network.cancel() }

        /// Load `url` with the pairing cookie installed for its host first
        /// (a no-op before the first pairing), and the device's name on the
        /// /pair request itself so the computer's list can show it.
        func load(_ url: URL, pairing: Bool) {
            guard let web = webView else { return }
            let server = self.server
            let host = url.host ?? self.host
            Task { @MainActor in
                await PairingSecret.install(into: web, server: server, host: host)
                var req = URLRequest(url: url)
                if pairing { req.setValue(ShellIdentity.deviceName, forHTTPHeaderField: "X-Fused-Device-Name") }
                web.load(req)
            }
        }

        @MainActor
        private func retryAfterNetworkChange() {
            guard navigationFailed, let web = webView else { return }
            webLog.info("network back; reloading \(web.url?.absoluteString ?? self.baseURL.absoluteString, privacy: .public)")
            // `reload()` on an error page re-requests the URL that failed;
            // with no URL at all (the very first load never landed) there is
            // nothing to reload, so ask again for what was requested.
            if web.url != nil { web.reload() } else { load(requestedURL ?? baseURL, pairing: requestedURL?.path == "/pair") }
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if navigationAction.targetFrame?.isMainFrame ?? true, let url = navigationAction.request.url {
                requestedURL = url
            }
            decisionHandler(.allow)
        }

        /// A navigation WebKit gave up on because another replaced it, or the
        /// page itself stopped it, is not a page that died on the network —
        /// reloading for it would interrupt the navigation that replaced it.
        private static func isRealFailure(_ error: Error) -> Bool {
            let e = error as NSError
            if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return false }
            if e.domain == "WebKitErrorDomain" && e.code == 102 { return false }  // frame load interrupted
            return true
        }

        @objc func pulled(_ sender: UIRefreshControl) {
            guard let web = webView else { sender.endRefreshing(); return }
            if web.url != nil { web.reload() } else { load(baseURL, pairing: false) }
        }

        // The pinned certificate is the only one accepted for this computer.
        func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                     completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
                  let trust = challenge.protectionSpace.serverTrust else {
                completionHandler(.performDefaultHandling, nil)
                return
            }
            // Pin for the paired computer ONLY (its name or its address).
            // Everything else a page loads (tile servers, fonts, APIs) keeps
            // the system trust store — cancelling those broke every app that
            // talks to the web.
            guard server.reaches(challenge.protectionSpace.host) else {
                completionHandler(.performDefaultHandling, nil)
                return
            }
            if TLSTrust.accepts(trust, fingerprint: server.fingerprint) {
                completionHandler(.useCredential, URLCredential(trust: trust))
            } else {
                completionHandler(.cancelAuthenticationChallenge, nil)
            }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
            if Self.isRealFailure(error) { failed() }
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
            if Self.isRealFailure(error) { failed() }
        }

        private func failed() {
            navigationFailed = true
            onNavigationFailed()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            webView.scrollView.refreshControl?.endRefreshing()
            navigationFailed = false
            webLog.info("finished \(webView.url?.absoluteString ?? "-", privacy: .public)")
            controller.location = webView.url
            controller.canGoBack = webView.canGoBack
            controller.title = webView.title ?? ""
            // The grid just loaded (paired, current): keep the pairing, and
            // refresh what the widgets and quick actions know.
            if let u = webView.url, u.path == "/" || u.path == "/index.html", let landed = u.host {
                let server = self.server
                Task { @MainActor in
                    await PairingSecret.capture(from: webView, server: server, host: landed)
                    onGridLoaded(landed)
                    ManifestSync.refresh(server: server, host: landed)
                }
            }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            controller.canGoBack = webView.canGoBack
            // A new document: whatever the old page was recording has no owner
            // any more — end it, or the microphone stays on with no way back.
            bridge.abandonPage()
        }

        // target=_blank links open in the same view.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url { webView.load(URLRequest(url: url)) }
            return nil
        }

        // Pages may ask for the microphone / camera. Over https they have a
        // secure context and WebKit asks us. Grant silently only to the paired
        // computer's own page in the main frame — the same line the capture
        // bridge draws; anything else (a third-party iframe inside an app)
        // gets WebKit's own prompt so nothing records behind the user's back.
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            if frame.isMainFrame, server.reaches(origin.host) {
                decisionHandler(.grant)
            } else {
                decisionHandler(.prompt)
            }
        }
    }
}
