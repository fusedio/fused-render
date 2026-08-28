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

struct WebView: UIViewRepresentable {
    let url: URL
    @Binding var pairURL: URL?
    let onNavigated: (URL) -> Void

    static let userAgentMarker: String = {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        return "FusedRender-iOS/\(v)"
    }()

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = true
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.isOpaque = false
        web.backgroundColor = .systemBackground
        // Append our marker to WebKit's own UA (it needs the default for
        // feature detection in pages).
        web.evaluateJavaScript("navigator.userAgent") { ua, _ in
            if let ua = ua as? String { web.customUserAgent = ua + " " + Self.userAgentMarker }
        }
        context.coordinator.webView = web
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

    func makeCoordinator() -> Coordinator { Coordinator(baseURL: url, onNavigated: onNavigated) }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var baseURL: URL
        weak var webView: WKWebView?
        let onNavigated: (URL) -> Void

        init(baseURL: URL, onNavigated: @escaping (URL) -> Void) {
            self.baseURL = baseURL
            self.onNavigated = onNavigated
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            if let u = webView.url { onNavigated(u) }
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
