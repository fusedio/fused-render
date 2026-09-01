// App state: the computers found on this Wi-Fi, the one we are connected to,
// and the servers this phone has paired with before (so a relaunch goes
// straight to the grid). Pairing itself is the webview loading the pair URL —
// the server sets a cookie in the webview's persistent store — so what is
// remembered here is the server's address plus, for https, the private CA it
// presents (TLSTrust.swift).
import Combine
import Foundation

struct Server: Identifiable, Codable, Equatable, Hashable {
    // One computer = one host. The same machine shows up as http:80 from
    // Bonjour and as https:443 (+ CA) once paired; port, scheme and CA are
    // how we reach it, not who it is — so identity, equality and hashing
    // use the host alone.
    static func == (a: Server, b: Server) -> Bool { a.host == b.host }
    func hash(into h: inout Hasher) { h.combine(host) }

    /// Bonjour service name, e.g. "Fused Render (render.fused.local)".
    var name: String
    /// Host to connect through. The listener only answers a Host header it
    /// advertises (lan.py `_host_ok`), so this is the advertised name or the
    /// IPv4 it published — never an arbitrary resolved endpoint.
    var host: String
    var port: Int
    /// "https" once the computer's CA is known and pinned; "http" until then
    /// (older records decode as http).
    var scheme: String = "http"
    /// The computer's private CA (DER) when scheme is https — the only anchor
    /// the app accepts for this host.
    var caDER: Data?

    var id: String { host }

    var isSecure: Bool { scheme == "https" && caDER != nil }

    var baseURL: URL {
        var c = URLComponents()
        c.scheme = scheme
        c.host = host
        c.port = (scheme == "https" ? port == 443 : port == 80) ? nil : port
        c.path = "/"
        return c.url!
    }

    enum CodingKeys: String, CodingKey { case name, host, port, scheme, caDER }

    init(name: String, host: String, port: Int, scheme: String = "http", caDER: Data? = nil) {
        self.name = name
        self.host = host
        self.port = port
        self.scheme = scheme
        self.caDER = caDER
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        host = try c.decode(String.self, forKey: .host)
        port = try c.decode(Int.self, forKey: .port)
        scheme = try c.decodeIfPresent(String.self, forKey: .scheme) ?? "http"
        caDER = try c.decodeIfPresent(Data.self, forKey: .caDER)
    }
}

@MainActor
final class AppModel: ObservableObject {
    /// Computers advertising the listener right now (Bonjour / probe).
    @Published var discovered: [Server] = []
    /// Servers this phone opened before, most recent first.
    @Published var known: [Server] = [] {
        didSet { persistKnown() }
    }
    /// The server the webview is showing, if any.
    @Published var current: Server?
    /// A URL the webview should load next (a pairing, or a deep link's page).
    @Published var pendingPairURL: URL?
    /// Why the last pairing attempt did not start, for the pair sheet.
    @Published var pairProblem: String?

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

    /// Open a computer this phone paired with before. The record carries the
    /// scheme and the pinned CA; nothing is learned from the network here.
    ///
    /// The app never trusts a CA it did not get from a QR code. Asking the
    /// computer itself (`/api/lan/tls`) would be trust-on-first-use: whoever
    /// answers to that name on the Wi-Fi first would get pinned. So a record
    /// paired over http before the computer had https stays http until the
    /// user scans again (Forget → Pair).
    func open(_ server: Server) {
        let server = known.first(where: { $0.host == server.host }) ?? server
        remember(server)
        current = server
    }

    /// A quick syntactic look at a scanned/pasted string: is this a pairing
    /// URL at all? The scanner uses it to decide whether to keep scanning.
    static func looksLikePairingCode(_ text: String) -> Bool {
        guard let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        return comps.path == "/pair" && comps.queryItems?.contains { $0.name == "t" && !($0.value ?? "").isEmpty } == true
    }

    /// The QR code (or a pasted URL) names `http://<host>[:port]/pair?t=…`,
    /// plus `ca` (the CA fingerprint) and `s` (the https port) from a server
    /// that has https. With those, the CA is fetched over http, checked against
    /// the fingerprint from the QR — the one channel an attacker on the Wi-Fi
    /// cannot touch — pinned, and the pairing happens over https, so the cookie
    /// lives on the https origin. A fingerprint that does not check out ends
    /// the pairing: there is no silent fall back to http. A code without `ca`
    /// (a computer that has no https) pairs over http.
    /// Async so the pair sheet stays up until the CA fetch settles: a
    /// fingerprint mismatch (or an unreachable computer) has to land as a
    /// visible error, not on a view that was already dismissed.
    func pair(with url: URL) async -> Bool {
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              comps.path == "/pair",
              let items = comps.queryItems,
              let token = items.first(where: { $0.name == "t" })?.value, !token.isEmpty,
              let host = comps.host, !host.isEmpty
        else {
            pairProblem = "That is not a Fused pairing code."
            return false
        }
        let httpPort = comps.port ?? 80
        let fingerprint = items.first(where: { $0.name == "ca" })?.value
        let httpsPort = items.first(where: { $0.name == "s" })?.value.flatMap(Int.init)
        pairProblem = nil
        var server = Server(name: discovered.first(where: { $0.host == host })?.name ?? host, host: host, port: httpPort)
        if let fingerprint, let httpsPort {
            guard let ca = await TLSTrust.fetchCA(host: host, httpPort: httpPort, expected: fingerprint) else {
                pairProblem = "The computer's certificate did not match this code. Try a fresh code from Preferences → Render local network."
                return false
            }
            server = Server(name: server.name, host: host, port: httpsPort, scheme: "https", caDER: ca)
        }
        var pairURL = URLComponents(url: server.baseURL, resolvingAgainstBaseURL: false)!
        pairURL.path = "/pair"
        pairURL.queryItems = [URLQueryItem(name: "t", value: token)]
        known.removeAll { $0.host == host }
        remember(server)
        pendingPairURL = pairURL.url
        current = server
        return true
    }

    /// A widget or quick action: `fusedrender://open?host&port&path`. Go to
    /// that computer and straight to the page, skipping the grid. The known
    /// record for that host carries the scheme and CA.
    ///
    /// Only a computer this phone already paired with. Any app can open a
    /// fusedrender:// URL; one naming a stranger's host would put the webview
    /// — bridge and all — on that host's page. The link's port/scheme are
    /// ignored too: the known record decides how the computer is reached.
    func open(deepLink url: URL) -> Bool {
        guard let link = DeepLink(url),
              let server = known.first(where: { $0.host == link.host }) else { return false }
        remember(server)
        var c = URLComponents(url: server.baseURL, resolvingAgainstBaseURL: false)!
        c.path = "/render"
        c.queryItems = [URLQueryItem(name: "path", value: link.path)]
        pendingPairURL = c.url  // the webview's "load this next" slot
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

    // MARK: persistence (UserDefaults — addresses and a PUBLIC CA certificate)

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
