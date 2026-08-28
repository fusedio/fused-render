// The webview that shows the grid and the apps. One persistent
// WKWebsiteDataStore (the default) is the whole pairing story: /pair sets the
// device cookie there once, and every later launch carries it.
//
// The user agent carries a marker (`FusedRender-iOS/<version>`) so the
// listener can tell this shell from a phone browser: lan.py serves the STOCK
// runtime.js to it instead of appending the phone-browser overrides
// (camera-photo capture etc.), leaving room for the native bridge to install
// its own `fused.capture` later.
import SwiftUI
import WebKit

/// The little the native chrome needs from the page: where it is, whether it
/// can go back, and two verbs. Owned by ConnectedView, filled in by WebView.
@MainActor
final class WebController: ObservableObject {
    weak var webView: WKWebView?
    @Published var location: URL?
    @Published var canGoBack = false

    func goBack() { webView?.goBack() }
    func load(_ url: URL) { webView?.load(URLRequest(url: url)) }
}

struct WebView: UIViewRepresentable {
    let url: URL
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
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = true
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.isOpaque = false
        web.backgroundColor = .systemBackground
        context.coordinator.webView = web
        controller.webView = web
        web.load(URLRequest(url: pairURL ?? url))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        if let pair = pairURL {
            // A fresh pairing request: load it once, then clear so a SwiftUI
            // re-render does not replay it.
            web.load(URLRequest(url: pair))
            DispatchQueue.main.async { pairURL = nil }
        } else if context.coordinator.baseURL != url {
            context.coordinator.baseURL = url
            web.load(URLRequest(url: url))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(baseURL: url, controller: controller) }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var baseURL: URL
        weak var webView: WKWebView?
        let controller: WebController
        let bridge = CaptureBridge()

        init(baseURL: URL, controller: WebController) {
            self.baseURL = baseURL
            self.controller = controller
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            controller.location = webView.url
            controller.canGoBack = webView.canGoBack
            // The grid just loaded (paired, current): refresh what the widgets
            // and quick actions know.
            if let u = webView.url, u.path == "/" || u.path == "/index.html", let host = u.host {
                ManifestSync.refresh(from: webView, server: Server(name: host, host: host, port: u.port ?? 80))
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

        // Pages may ask for the microphone / camera (apps that record). The
        // insecure-context rule does not apply inside WKWebView the way it does
        // in Safari; grant, and let iOS's own permission prompt decide.
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(.grant)
        }
    }
}
