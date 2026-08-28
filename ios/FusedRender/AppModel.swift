// App state: the computers found on this Wi-Fi, the one we are connected to,
// and the servers this phone has paired with before (so a relaunch goes
// straight to the grid). Pairing itself is the webview loading the pair URL —
// the server sets a cookie in the webview's persistent store — so the only
// thing worth remembering here is the server's address.
import Combine
import Foundation

struct Server: Identifiable, Codable, Equatable, Hashable {
    /// Bonjour service name, e.g. "Fused Render (render.fused.local)".
    var name: String
    /// Host to connect through. The listener only answers a Host header it
    /// advertises (lan.py `_host_ok`), so this is the advertised name or the
    /// IPv4 it published — never an arbitrary resolved endpoint.
    var host: String
    var port: Int

    var id: String { "\(host):\(port)" }

    var baseURL: URL {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = port == 80 ? nil : port
        c.path = "/"
        return c.url!
    }
}

@MainActor
final class AppModel: ObservableObject {
    /// Computers advertising the listener right now (Bonjour).
    @Published var discovered: [Server] = []
    /// Servers this phone opened before, most recent first.
    @Published var known: [Server] = [] {
        didSet { persistKnown() }
    }
    /// The server the webview is showing, if any.
    @Published var current: Server?
    /// A pairing the user asked for: the webview loads this URL, the server
    /// replies with the cookie and a redirect to the grid.
    @Published var pendingPairURL: URL?

    private let discovery = Discovery()
    private var bag = Set<AnyCancellable>()

    init() {
        known = Self.loadKnown()
        discovery.$servers
            .receive(on: DispatchQueue.main)
            .assign(to: &$discovered)
        discovery.start()
        // A phone that paired before goes straight back in.
        if let last = known.first { current = last }
    }

    func open(_ server: Server) {
        remember(server)
        current = server
    }

    /// The QR code (or a pasted URL) names `http://<host>[:port]/pair?t=…`.
    /// We keep the host it names — that is the advertised name and the one the
    /// listener will answer — and let the webview do the pairing.
    func pair(with url: URL) -> Bool {
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              comps.path == "/pair",
              comps.queryItems?.contains(where: { $0.name == "t" && !($0.value ?? "").isEmpty }) == true,
              let host = comps.host, !host.isEmpty
        else { return false }
        let server = Server(name: discovered.first(where: { $0.host == host })?.name ?? host,
                            host: host, port: comps.port ?? 80)
        remember(server)
        pendingPairURL = url
        current = server
        return true
    }

    func forget(_ server: Server) {
        known.removeAll { $0 == server }
        if current == server { current = nil }
    }

    func disconnect() {
        current = nil
        pendingPairURL = nil
    }

    private func remember(_ server: Server) {
        known.removeAll { $0 == server }
        known.insert(server, at: 0)
    }

    // MARK: persistence (UserDefaults — addresses only, nothing secret)

    private static let knownKey = "fused.knownServers"

    private static func loadKnown() -> [Server] {
        guard let data = UserDefaults.standard.data(forKey: knownKey),
              let servers = try? JSONDecoder().decode([Server].self, from: data) else { return [] }
        return servers
    }

    private func persistKnown() {
        if let data = try? JSONEncoder().encode(known) {
            UserDefaults.standard.set(data, forKey: Self.knownKey)
        }
    }
}
