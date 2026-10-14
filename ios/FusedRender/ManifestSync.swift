// Keeps the shared manifest (Shared/AppManifest.swift) current: whenever the
// grid loads, fetch the same /api/lan/apps the grid shows — with the webview's
// cookie — cache each preview.png, write apps.json, and tell WidgetKit and the
// Home Screen quick actions (Bots first, then the apps). Best-effort
// throughout: a failed refresh leaves the last manifest in place.
import Foundation
import UIKit
import WebKit
import WidgetKit
import os

private let log = Logger(subsystem: "io.fused.render", category: "manifest")

@MainActor
enum ManifestSync {
    private static var lastRefresh: Date = .distantPast
    private static var lastHost = ""
    /// Bumped per refresh; a run that finished for an earlier one must not put its
    /// host's Bots shortcut back over the computer the user has since switched to.
    private static var generation = 0

    static func refresh(from webView: WKWebView, server: Server) {
        // Throttled per computer: switching computers within the window still refreshes.
        guard server.host != lastHost || Date().timeIntervalSince(lastRefresh) > 5 else { return }
        lastRefresh = Date()
        lastHost = server.host
        generation += 1
        let gen = generation
        Task { await run(webView: webView, server: server, generation: gen) }
    }

    private static func run(webView: WKWebView, server: Server, generation gen: Int) async {
        let cookies = await webView.configuration.websiteDataStore.httpCookieStore.allCookies()
        let cookieHeader = cookies
            .filter { server.host.hasSuffix($0.domain.trimmingCharacters(in: CharacterSet(charactersIn: "."))) }
            .map { "\($0.name)=\($0.value)" }
            .joined(separator: "; ")
        let pinned = PinnedSession(caDER: server.caDER)
        var req = URLRequest(url: server.baseURL.appendingPathComponent("api/lan/apps"))
        req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        req.setValue(WebView.userAgentMarker, forHTTPHeaderField: "User-Agent")
        req.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (data, response) = try? await pinned.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            log.info("manifest refresh skipped (not paired or offline)")
            return
        }

        var apps: [ManifestApp] = []
        for row in payload.apps {
            var app = ManifestApp(host: server.host, port: server.port, scheme: server.scheme, name: row.name,
                                  title: row.title, path: row.path, url: row.url, tag: row.tag,
                                  recency: row.recency, previewFile: nil)
            if let preview = row.preview {
                app.previewFile = await cachePreview(preview, for: app, cookieHeader: cookieHeader, session: pinned)
            }
            apps.append(app)
        }
        // Keep apps from OTHER computers the phone knows; replace this one's.
        let others = Manifest.load().filter { !($0.host == server.host && $0.port == server.port) }
        let merged = (apps + others).sorted { $0.recency > $1.recency }
        Manifest.save(merged)
        WidgetCenter.shared.reloadAllTimelines()
        // The manifest is right for any computer; the Bots shortcut names ONE, the current one.
        if gen == generation {
            installQuickActions(merged, host: server.host)
        }
        log.info("manifest: \(apps.count) apps from \(server.host, privacy: .public)")
    }

    /// Download preview.png once per (app, mtime-less) — re-fetched on every
    /// refresh but only rewritten when the bytes changed; downscaled to widget
    /// size so the container stays small.
    private static func cachePreview(_ relative: String, for app: ManifestApp, cookieHeader: String,
                                     session: PinnedSession) async -> String? {
        guard let url = URL(string: relative, relativeTo: app.baseURL)?.absoluteURL else { return nil }
        var req = URLRequest(url: url)
        req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        req.setValue(WebView.userAgentMarker, forHTTPHeaderField: "User-Agent")
        guard let (data, response) = try? await session.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let image = UIImage(data: data) else { return nil }
        let side: CGFloat = 400
        let scale = min(1, side / max(image.size.width, image.size.height))
        let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let small = UIGraphicsImageRenderer(size: size).image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }
        guard let png = small.pngData() else { return nil }
        let name = app.id.data(using: .utf8)!.base64EncodedString()
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "+", with: "-") + ".png"
        let dest = FusedShared.containerURL.appendingPathComponent("previews/\(name)")
        try? FileManager.default.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
        if (try? Data(contentsOf: dest)) != png { try? png.write(to: dest, options: .atomic) }
        return name
    }

    /// Long-press the app icon → "Bots" (the bots list on `host`, the computer
    /// whose grid just loaded — the most recent one), then the three most
    /// recent apps. No host, no Bots item: the link would have nowhere to go.
    private static func installQuickActions(_ apps: [ManifestApp], host: String?) {
        var items: [UIApplicationShortcutItem] = []
        if let host, !host.isEmpty {
            items.append(UIApplicationShortcutItem(
                type: "io.fused.render.bots",
                localizedTitle: "Bots",
                localizedSubtitle: nil,
                icon: UIApplicationShortcutIcon(systemImageName: "sparkles"),
                userInfo: ["url": DeepLink.bots(host: host).absoluteString as NSSecureCoding]
            ))
        }
        items += apps.prefix(3).map { app in
            UIApplicationShortcutItem(
                type: "io.fused.render.open",
                localizedTitle: app.label,
                localizedSubtitle: app.tag,
                icon: UIApplicationShortcutIcon(systemImageName: "sparkle"),
                userInfo: ["url": app.deepLink.absoluteString as NSSecureCoding]
            )
        }
        UIApplication.shared.shortcutItems = items
    }

    private struct Payload: Decodable {
        struct Row: Decodable {
            let name: String
            let title: String?
            let tag: String?
            let path: String
            let url: String
            let recency: Double
            let preview: String?
        }
        let apps: [Row]
    }
}
