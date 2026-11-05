// Keeps the shared manifest (Shared/AppManifest.swift) current: whenever the
// grid loads, fetch the same /api/lan/apps the grid shows — with the pairing
// cookie from the Keychain (PairingSecret) over the pinned session — cache
// each preview.png, write apps.json, and tell WidgetKit and the
// Home Screen quick actions (Bots and Tasks first, then the apps). Best-effort
// throughout: a failed refresh leaves the last manifest in place.
import Foundation
import UIKit
import WidgetKit
import os

private let log = Logger(subsystem: "io.fused.render", category: "manifest")

@MainActor
enum ManifestSync {
    private static var lastRefresh: Date = .distantPast
    private static var lastHost = ""
    /// Bumped per refresh; a run that finished for an earlier one must not put its
    /// host's Bots and Tasks shortcuts back over the computer the user has since switched to.
    private static var generation = 0

    /// `host` is the one the grid just loaded on (the name, or the fallback
    /// address): the refresh talks to the computer the same way.
    static func refresh(server: Server, host: String, port: Int) {
        // Throttled per computer: switching computers within the window still refreshes.
        guard server.fingerprint != lastHost || Date().timeIntervalSince(lastRefresh) > 5 else { return }
        lastRefresh = Date()
        lastHost = server.fingerprint
        generation += 1
        let gen = generation
        Task { await run(server: server, host: host, port: port, generation: gen) }
    }

    private static func run(server: Server, host: String, port: Int, generation gen: Int) async {
        guard let cookieHeader = PairingSecret.cookieHeader(for: server) else {
            log.info("manifest refresh skipped (no pairing secret yet)")
            return
        }
        let pinned = PinnedSession(fingerprint: server.fingerprint)
        let base = server.url(host: host, port: port)
        var req = URLRequest(url: base.appendingPathComponent("api/lan/apps"))
        req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        req.setValue(ShellIdentity.userAgentMarker, forHTTPHeaderField: "User-Agent")
        req.cachePolicy = .reloadIgnoringLocalCacheData
        guard let (data, response) = try? await pinned.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else {
            log.info("manifest refresh skipped (not paired or offline)")
            return
        }

        var apps: [ManifestApp] = []
        for row in payload.apps {
            var app = ManifestApp(host: server.host, port: server.port, scheme: "https", name: row.name,
                                  title: row.title, path: row.path, url: row.url, tag: row.tag,
                                  recency: row.recency, previewFile: nil, fingerprint: server.fingerprint)
            if let preview = row.preview {
                // From the host the grid actually loaded on (the name, or the
                // fallback address), not the record's name.
                app.previewFile = await cachePreview(preview, for: app, base: base,
                                                     cookieHeader: cookieHeader, session: pinned)
            }
            apps.append(app)
        }
        // Keep apps from OTHER computers the phone knows; replace this one's
        // (by certificate; by host and port for manifests older builds wrote).
        let others = Manifest.load().filter { app in
            if let fp = app.fingerprint { return fp != server.fingerprint }
            return !(app.host == server.host && app.port == server.port)
        }
        let merged = (apps + others).sorted { $0.recency > $1.recency }
        Manifest.save(merged)
        WidgetCenter.shared.reloadAllTimelines()
        // The manifest is right for any computer; the Bots and Tasks shortcuts name ONE, the current one.
        if gen == generation {
            installQuickActions(merged, host: server.host)
        }
        log.info("manifest: \(apps.count) apps from \(server.host, privacy: .public)")
    }

    /// Download preview.png once per (app, mtime-less) — re-fetched on every
    /// refresh but only rewritten when the bytes changed; downscaled to widget
    /// size so the container stays small.
    private static func cachePreview(_ relative: String, for app: ManifestApp, base: URL, cookieHeader: String,
                                     session: PinnedSession) async -> String? {
        guard let url = URL(string: relative, relativeTo: base)?.absoluteURL else { return nil }
        var req = URLRequest(url: url)
        req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        req.setValue(ShellIdentity.userAgentMarker, forHTTPHeaderField: "User-Agent")
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

    /// Long-press the app icon → "Bots" and "Tasks" (the bots and tasks lists
    /// on `host`, the computer whose grid just loaded — the most recent one),
    /// then the three most recent apps. No host, no Bots or Tasks item: the
    /// link would have nowhere to go.
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
            items.append(UIApplicationShortcutItem(
                type: "io.fused.render.tasks",
                localizedTitle: "Tasks",
                localizedSubtitle: nil,
                icon: UIApplicationShortcutIcon(systemImageName: "checklist"),
                userInfo: ["url": DeepLink.tasks(host: host).absoluteString as NSSecureCoding]
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
