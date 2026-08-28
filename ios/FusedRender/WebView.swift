// The webview that shows the grid and the apps. One persistent
// WKWebsiteDataStore (the default) is the whole pairing story: /pair sets the
// device cookie there once, and every later launch carries it.
//
// The user agent carries a marker (`FusedRender-iOS/<version>`) so the
// listener can tell this shell from a phone browser: lan.py serves the STOCK
// runtime.js to it and the native bridge (CaptureBridge.swift) installs its
// own `fused.capture` on top.
//
// Over https the server presents a certificate from its own private CA; the
// navigation delegate accepts exactly that CA for exactly this host
// (TLSTrust.swift) — which is what gives pages a secure context, and with it
// the microphone and clipboard the same pages lack on plain http.
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

struct WebView: UIViewRepresentable {
    let server: Server
    @Binding var pairURL: URL?
    let controller: WebController

    static let userAgentMarker: String = {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        return "FusedRender-iOS/\(v)"
    }()

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        // The marker rides on WebKit's own UA from the very first request (the
        // pairing one included) — setting customUserAgent after creation was
        // too late, and the server named the device "iPhone · Safari".
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
                WKUserScript(source: source, injectionTime: .atDocumentEnd, forMainFrameOnly: false))
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
        web.load(URLRequest(url: pairURL ?? server.baseURL))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        context.coordinator.server = server
        context.coordinator.bridge.server = server
        if let pair = pairURL {
            // A fresh pairing request: load it once, then clear so a SwiftUI
            // re-render does not replay it. Guarded against the same URL being
            // seen twice before the async clear lands.
            if context.coordinator.lastPending != pair {
                context.coordinator.lastPending = pair
                webLog.info("load pending \(pair.absoluteString, privacy: .public)")
                web.load(URLRequest(url: pair))
            }
            DispatchQueue.main.async { pairURL = nil }
        } else if context.coordinator.baseURL != server.baseURL {
            context.coordinator.baseURL = server.baseURL
            webLog.info("load base \(server.baseURL.absoluteString, privacy: .public)")
            web.load(URLRequest(url: server.baseURL))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(server: server, controller: controller) }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var server: Server
        var baseURL: URL
        var lastPending: URL?
        weak var webView: WKWebView?
        let controller: WebController
        let bridge = CaptureBridge()

        init(server: Server, controller: WebController) {
            self.server = server
            self.baseURL = server.baseURL
            self.controller = controller
        }

        @objc func pulled(_ sender: UIRefreshControl) {
            guard let web = webView else { sender.endRefreshing(); return }
            if web.url != nil { web.reload() } else { web.load(URLRequest(url: baseURL)) }
        }

        // The server's private CA is the only anchor for this host.
        func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                     completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
                  let trust = challenge.protectionSpace.serverTrust else {
                completionHandler(.performDefaultHandling, nil)
                return
            }
            if let ca = server.caDER, TLSTrust.accepts(trust, caDER: ca, host: challenge.protectionSpace.host) {
                completionHandler(.useCredential, URLCredential(trust: trust))
            } else {
                completionHandler(.cancelAuthenticationChallenge, nil)
            }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            webView.scrollView.refreshControl?.endRefreshing()
            webLog.info("finished \(webView.url?.absoluteString ?? "-", privacy: .public)")
            controller.location = webView.url
            controller.canGoBack = webView.canGoBack
            controller.title = webView.title ?? ""
            // The grid just loaded (paired, current): refresh what the widgets
            // and quick actions know.
            if let u = webView.url, u.path == "/" || u.path == "/index.html" {
                ManifestSync.refresh(from: webView, server: server)
            }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            controller.canGoBack = webView.canGoBack
        }

        // target=_blank links open in the same view.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url { webView.load(URLRequest(url: url)) }
            return nil
        }

        // Pages may ask for the microphone / camera. Over https they have a
        // secure context and WebKit asks us; grant, and let iOS's own
        // permission prompt decide.
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(.grant)
        }
    }
}
