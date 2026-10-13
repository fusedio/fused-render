// What the app and its widget extension share: the list of Fused apps the
// phone last saw (name, where they live, a cached preview), the deep link that
// opens one, and the monogram colour rule the grid uses. Lives in the App
// Group container so the widget can draw without the network and the app can
// refresh it whenever the grid loads.
import CryptoKit
import Foundation
import SwiftUI

enum FusedShared {
    static let appGroup = "group.io.fused.render.ios"
    static let scheme = "fusedrender"

    /// The App Group container, or — when the entitlement is missing (an
    /// unsigned build) — the app's own caches dir so the app still works
    /// alone. Widgets then simply see nothing.
    static var containerURL: URL {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroup)
            ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
    }
}

/// One Fused app as the grid lists it (fused_render/lan.py `lan_apps`).
struct ManifestApp: Codable, Identifiable, Hashable {
    var host: String
    var port: Int
    /// "http" or "https" — how the app reaches this computer (TLSTrust.swift).
    var scheme: String = "http"
    var name: String
    var title: String?
    /// The app folder on the computer.
    var path: String
    /// The `/render?path=…` URL (relative) the grid opens.
    var url: String
    var tag: String?
    var recency: Double
    /// Cached preview.png in `previews/`, when the app has one.
    var previewFile: String?

    /// A plain hex digest, not "host:port|/path": AppIntents persists a chosen
    /// entity by its id, and ids carrying "/" and ":" were not round-tripping —
    /// the widget kept its previous app after every edit.
    var id: String {
        let key = "\(host):\(port)|\(path)"
        return SHA256.hash(data: Data(key.utf8)).prefix(16).map { String(format: "%02x", $0) }.joined()
    }
    var label: String { title?.isEmpty == false ? title! : name }

    var baseURL: URL {
        var c = URLComponents()
        c.scheme = scheme
        c.host = host
        c.port = (scheme == "https" ? port == 443 : port == 80) ? nil : port
        c.path = "/"
        return c.url!
    }

    /// Where the page lives: the grid's relative URL resolved on the server.
    var pageURL: URL { URL(string: url, relativeTo: baseURL)!.absoluteURL }

    /// `fusedrender://open?host=…&port=…&scheme=…&path=<entry html>` — widgets
    /// and quick actions carry this; the app routes it straight to the page.
    var deepLink: URL {
        var c = URLComponents()
        c.scheme = FusedShared.scheme
        c.host = "open"
        let entry = URLComponents(string: url)?.queryItems?.first(where: { $0.name == "path" })?.value ?? path
        c.queryItems = [
            .init(name: "host", value: host),
            .init(name: "port", value: String(port)),
            .init(name: "scheme", value: scheme),
            .init(name: "path", value: entry),
        ]
        return c.url!
    }

    var previewURL: URL? {
        previewFile.map { FusedShared.containerURL.appendingPathComponent("previews/\($0)") }
    }
}

/// `fusedrender://open?host=…` plus one of: `path=<entry html>` (an app page,
/// `/render?path=…`), `page=bots` (the bots list, `/bots`) or `bot=<id>` (one
/// bot's chat, `/bots/<id>`). Port and scheme ride along on app links but the
/// app ignores them (AppModel.open(deepLink:)).
struct DeepLink {
    enum Target: Equatable {
        case app(path: String)
        case bots
        case bot(id: String)
    }

    let host: String
    let port: Int
    let scheme: String
    let target: Target

    init?(_ url: URL) {
        guard url.scheme == FusedShared.scheme, url.host == "open",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let host = items.first(where: { $0.name == "host" })?.value, !host.isEmpty
        else { return nil }
        let value: (String) -> String? = { name in items.first(where: { $0.name == name })?.value }
        // A bot id becomes a path segment: anything outside [A-Za-z0-9_-]
        // refuses the whole link rather than being cleaned up into another id.
        if let bot = value("bot") {
            guard Self.isBotID(bot) else { return nil }
            self.target = .bot(id: bot)
        } else if let page = value("page") {
            guard page == "bots" else { return nil }
            self.target = .bots
        } else if let path = value("path"), !path.isEmpty {
            self.target = .app(path: path)
        } else {
            return nil
        }
        self.host = host
        self.scheme = value("scheme") ?? "http"
        self.port = Int(value("port") ?? "") ?? (scheme == "https" ? 443 : 80)
    }

    private static let botIDCharacters = CharacterSet(
        charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

    static func isBotID(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 128 && id.unicodeScalars.allSatisfy { botIDCharacters.contains($0) }
    }

    /// `fusedrender://open?host=…&page=bots` — the "Bots" quick action.
    static func bots(host: String) -> URL {
        link(host: host, [.init(name: "page", value: "bots")])
    }

    /// `fusedrender://open?host=…&bot=<id>`; nil for an id the parser would refuse.
    static func bot(_ id: String, host: String) -> URL? {
        guard isBotID(id) else { return nil }
        return link(host: host, [.init(name: "bot", value: id)])
    }

    private static func link(host: String, _ extra: [URLQueryItem]) -> URL {
        var c = URLComponents()
        c.scheme = FusedShared.scheme
        c.host = "open"
        c.queryItems = [URLQueryItem(name: "host", value: host)] + extra
        return c.url!
    }
}

enum Manifest {
    private static var fileURL: URL { FusedShared.containerURL.appendingPathComponent("apps.json") }

    static func load() -> [ManifestApp] {
        guard let data = try? Data(contentsOf: fileURL),
              let apps = try? JSONDecoder().decode([ManifestApp].self, from: data) else { return [] }
        return apps
    }

    static func save(_ apps: [ManifestApp]) {
        let dir = FusedShared.containerURL
        try? FileManager.default.createDirectory(at: dir.appendingPathComponent("previews"), withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(apps) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }
}

/// The grid's monogram rule (frontend/src/lan/App.tsx `hueOf`): a hue from the
/// name, so the same app is the same colour on the phone, in the widget, and
/// in the grid.
enum Monogram {
    static func hue(_ name: String) -> Double {
        var h: UInt32 = 0
        for u in name.unicodeScalars { h = h &* 31 &+ UInt32(truncatingIfNeeded: u.value) }
        return Double(h % 360)
    }

    static func background(_ name: String) -> Color {
        Color(hue: hue(name) / 360, saturation: 0.45, brightness: 0.32)
    }

    static func foreground(_ name: String) -> Color {
        Color(hue: hue(name) / 360, saturation: 0.25, brightness: 0.95)
    }

    static func initial(_ label: String) -> String {
        String(label.trimmingCharacters(in: .whitespaces).first.map { Character($0.lowercased()) } ?? "?")
    }
}
