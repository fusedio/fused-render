// App state: the computer we are connected to and the computers this phone
// has paired with before (so a relaunch goes straight to the grid). Pairing
// itself is the webview loading the pair URL — the server sets a cookie — and
// what is remembered here is how to reach the computer (its local name, the
// port, an address to fall back to) and who it is (the fingerprint of the
// certificate it presents, TLSTrust.swift). The pairing secret itself lives
// in the Keychain (PairingSecret.swift), keyed on that fingerprint.
import Combine
import Foundation

struct Server: Identifiable, Codable, Equatable, Hashable {
    // One computer = one certificate. Its name and address are how we reach
    // it, not who it is: both can change under us (a rename, a new DHCP
    // lease, a network that filters mDNS), and the pairing must survive
    // that. So identity, equality and hashing use the fingerprint alone.
    static func == (a: Server, b: Server) -> Bool { a.fingerprint == b.fingerprint }
    func hash(into h: inout Hasher) { h.combine(fingerprint) }

    /// What the UI calls it: the local name without `.local`, else the host.
    var name: String
    /// The computer's OS-advertised local name (`studio.local`) as the pairing
    /// code carried it, or its address when the computer had no name.
    var host: String
    var port: Int
    /// SHA-256 (hex) of the certificate the listener presents — the one thing
    /// the app trusts about this computer.
    var fingerprint: String
    /// The address the pairing code carried beside the name, and later the
    /// last address the computer actually answered on: where to look when the
    /// name does not resolve.
    var lastIP: String?

    var id: String { fingerprint }

    var baseURL: URL { url(host: host) }

    /// The base URL through `host` — the name, or the fallback address.
    func url(host: String) -> URL {
        var c = URLComponents()
        c.scheme = "https"
        c.host = host
        c.port = port
        c.path = "/"
        return c.url!
    }

    /// The hosts the computer may answer on, the name first.
    var candidateHosts: [String] {
        var hosts = [host]
        if let ip = lastIP, ip != host { hosts.append(ip) }
        return hosts
    }

    /// Is `host` one of the ways this computer is reached?
    func reaches(_ host: String) -> Bool { candidateHosts.contains(host) }

    enum CodingKeys: String, CodingKey { case name, host, port, fingerprint, lastIP }

    init(name: String, host: String, port: Int, fingerprint: String, lastIP: String? = nil) {
        self.name = name
        self.host = host
        self.port = port
        self.fingerprint = fingerprint
        self.lastIP = lastIP
    }

    static func displayName(for host: String) -> String {
        host.hasSuffix(".local") ? String(host.dropLast(".local".count)) : host
    }
}

@MainActor
final class AppModel: ObservableObject {
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
    /// A pairing that started but has not LANDED: the webview is loading
    /// /pair, and only the redirect to the grid proves the token was live and
    /// the cookie set. Remembered into `known` then, not before — an expired
    /// code must not leave a cookieless computer that auto-opens next launch.
    private var unconfirmed: Server?
    /// The token of that pairing, so a fallback to the computer's address
    /// (the name did not resolve on this network) can re-issue the same
    /// /pair there: a token is only spent when the server receives it.
    private var pendingToken: String?

    init() {
        known = Self.loadKnown()
        // A phone that paired before goes straight back in.
        if let last = known.first { current = last }
    }

    /// Open a computer this phone paired with before. The record carries the
    /// fingerprint and the fallback address; nothing is learned from the
    /// network here, and nothing is trusted that did not come from a code.
    func open(_ server: Server) {
        let server = known.first(where: { $0 == server }) ?? server
        // Navigating away from an in-flight pairing abandons it: only the
        // /pair load that pair() itself started may confirm.
        unconfirmed = nil
        pendingToken = nil
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

    /// The QR code (or a pasted URL) names
    /// `https://<host>:<port>/pair?t=…&fp=<fingerprint>[&ip=<address>]`.
    /// Nothing is fetched first: the fingerprint IS the trust, checked on the
    /// handshake of the /pair load itself (WebView's challenge handler), and
    /// `ip` is kept as the address to try when the name does not resolve.
    func pair(with url: URL) -> Bool {
        guard let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              comps.path == "/pair",
              let items = comps.queryItems,
              let token = items.first(where: { $0.name == "t" })?.value, !token.isEmpty,
              let host = comps.host, !host.isEmpty
        else {
            pairProblem = "That is not a Fused pairing code."
            return false
        }
        guard comps.scheme == "https",
              let fingerprint = items.first(where: { $0.name == "fp" })?.value?.lowercased(),
              fingerprint.count == 64, fingerprint.allSatisfy(\.isHexDigit) else {
            pairProblem = "This code comes from an older Fused Render. Update it on the computer and scan again."
            return false
        }
        let port = comps.port ?? 443
        let ip = items.first(where: { $0.name == "ip" })?.value.flatMap { $0.isEmpty ? nil : $0 }
        pairProblem = nil
        let server = Server(name: Server.displayName(for: host), host: host, port: port,
                            fingerprint: fingerprint, lastIP: ip)
        unconfirmed = server
        pendingToken = token
        pendingPairURL = Self.pairURL(server, host: host, token: token)
        current = server
        return true
    }

    private static func pairURL(_ server: Server, host: String, token: String) -> URL {
        var c = URLComponents(url: server.url(host: host), resolvingAgainstBaseURL: false)!
        c.path = "/pair"
        c.queryItems = [URLQueryItem(name: "t", value: token)]
        return c.url!
    }

    /// The pairing's /pair never reached the computer (the name did not
    /// resolve, or Local Network access was only just allowed), and the probe
    /// now finds it on `host`: send the same code there. A token is only
    /// spent when the server receives it. Returns false when no pairing is
    /// pending.
    @discardableResult
    func retryPairing(on host: String) -> Bool {
        guard let server = unconfirmed, let token = pendingToken, server.reaches(host) else { return false }
        pendingPairURL = Self.pairURL(server, host: host, token: token)
        return true
    }

    /// The webview landed on the grid for `host`. If a pairing was in flight
    /// for that computer, it is now proven (the /pair redirect only happens
    /// after the cookie is set) — remember it.
    func confirmPairing(host: String) {
        guard let server = unconfirmed, server.reaches(host) else { return }
        unconfirmed = nil
        pendingToken = nil
        remember(server)
    }

    /// The computer answered on `host` (Reachability): keep an address that
    /// worked as the fallback for next time.
    func noteReached(_ server: Server, host: String) {
        guard host != server.host, host != server.lastIP,
              let i = known.firstIndex(of: server) else { return }
        known[i].lastIP = host
        if current == server { current = known[i] }
    }

    /// A widget or quick action: `fusedrender://open?host&…` naming an app
    /// (`path`), the bots list (`page=bots`) or one bot (`bot=<id>`). Go to
    /// that computer and straight to the page, skipping the grid.
    ///
    /// Only a computer this phone already paired with. Any app can open a
    /// fusedrender:// URL; one naming a stranger's host would put the webview
    /// — bridge and all — on that host's page. The link's port/scheme are
    /// ignored too: the known record decides how the computer is reached. A
    /// bot id is a path segment, so DeepLink refuses any outside [A-Za-z0-9_-].
    func open(deepLink url: URL) -> Bool {
        guard let link = DeepLink(url),
              let server = known.first(where: { $0.reaches(link.host) }) else { return false }
        unconfirmed = nil  // same rule as open(_:) — leaving the pairing abandons it
        pendingToken = nil
        remember(server)
        var c = URLComponents(url: server.baseURL, resolvingAgainstBaseURL: false)!
        switch link.target {
        case .app(let path):
            c.path = "/render"
            c.queryItems = [URLQueryItem(name: "path", value: path)]
        case .bots:
            c.path = "/bots"
        case .bot(let id):
            c.path = "/bots/\(id)"
        }
        pendingPairURL = c.url  // the webview's "load this next" slot
        current = server
        return true
    }

    func forget(_ server: Server) {
        PairingSecret.delete(for: server)
        known.removeAll { $0 == server }
        if current == server { current = nil }
    }

    func disconnect() {
        current = nil
        pendingPairURL = nil
        unconfirmed = nil
        pendingToken = nil
    }

    private func remember(_ server: Server) {
        known.removeAll { $0 == server }
        known.insert(server, at: 0)
    }

    // MARK: persistence (UserDefaults — names, addresses and a PUBLIC fingerprint)

    private static let knownKey = "fused.knownServers"

    private static func loadKnown() -> [Server] {
        guard let data = UserDefaults.standard.data(forKey: knownKey) else { return [] }
        // Records from the private-CA era carry no fingerprint and cannot be
        // used (the computer no longer presents that CA): drop them, the
        // phone pairs again with one scan.
        struct Loose: Decodable { let name: String; let host: String; let port: Int; let fingerprint: String?; let lastIP: String? }
        guard let rows = try? JSONDecoder().decode([Loose].self, from: data) else { return [] }
        return rows.compactMap { r in
            guard let fp = r.fingerprint else { return nil }
            return Server(name: r.name, host: r.host, port: r.port, fingerprint: fp, lastIP: r.lastIP)
        }
    }

    private func persistKnown() {
        if let data = try? JSONEncoder().encode(known) {
            UserDefaults.standard.set(data, forKey: Self.knownKey)
        }
    }
}
