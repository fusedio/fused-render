// Bonjour discovery of the listener. lan.py registers two `_http._tcp` services
// named "Fused Render (<host>)" whose `server` is the host to connect through
// (render.fused.local / render.local) and whose address is the computer's LAN
// IPv4. We browse, resolve each, and keep the ADVERTISED host — the listener's
// Host allowlist accepts exactly those names and that address, so connecting
// through some other resolved endpoint (an IPv6, a second interface) would get
// a 404 and a blank page.
import Foundation
import Network
import os

private let log = Logger(subsystem: "io.fused.render", category: "discovery")

final class Discovery: ObservableObject {
    @Published private(set) var servers: [Server] = []

    private var browser: NWBrowser?
    private var connections: [String: NWConnection] = [:]
    private var found: [String: Server] = [:]  // keyed by service name

    func start() {
        guard browser == nil else { return }
        let params = NWParameters()
        params.includePeerToPeer = true
        let b = NWBrowser(for: .bonjour(type: "_http._tcp", domain: "local."), using: params)
        b.stateUpdateHandler = { [weak self] state in
            log.info("browser state: \(String(describing: state), privacy: .public)")
            if case .failed = state { b.cancel(); self?.browser = nil }
        }
        b.browseResultsChangedHandler = { [weak self] results, _ in
            log.info("browse results: \(results.count)")
            self?.handle(results)
        }
        b.start(queue: .main)
        browser = b
        startProbing()
    }

    func stop() {
        browser?.cancel()
        browser = nil
        connections.values.forEach { $0.cancel() }
        connections.removeAll()
        probeTimer?.invalidate()
        probeTimer = nil
    }

    // MARK: direct probe
    //
    // Bonjour BROWSING is not always available where NAME RESOLUTION is: the
    // simulator's browser reports ready and never yields a result, and some
    // networks filter the multicast queries browsing needs while still letting
    // the phone resolve `.local` names (a different, single query). The
    // listener advertises fixed names (lan.py HOSTNAME / ALIAS_HOSTNAME), so
    // asking each one "are you there?" over HTTP is a complete fallback: any
    // HTTP answer at all — 403 for an unpaired phone — means the listener.

    private static let wellKnown = ["render.fused.local", "render.local"]
    private var probeTimer: Timer?
    private var probed: [String: Server] = [:]

    private func startProbing() {
        probe()
        probeTimer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in self?.probe() }
    }

    private func probe() {
        for host in Self.wellKnown {
            var req = URLRequest(url: URL(string: "http://\(host)/")!)
            req.httpMethod = "HEAD"
            req.timeoutInterval = 2
            req.cachePolicy = .reloadIgnoringLocalCacheData
            URLSession.shared.dataTask(with: req) { [weak self] _, response, _ in
                guard let self, let http = response as? HTTPURLResponse else {
                    DispatchQueue.main.async { self?.probed.removeValue(forKey: host); self?.publish() }
                    return
                }
                let port = http.url?.port ?? 80
                DispatchQueue.main.async {
                    self.probed[host] = Server(name: "Fused Render (\(host))", host: host, port: port)
                    self.publish()
                }
            }.resume()
        }
    }

    private func handle(_ results: Set<NWBrowser.Result>) {
        var live = Set<String>()
        for result in results {
            guard case let .service(name, type, domain, _) = result.endpoint else { continue }
            log.info("service \(name, privacy: .public) \(type, privacy: .public) \(domain, privacy: .public)")
            guard name.hasPrefix("Fused Render") else { continue }
            live.insert(name)
            if found[name] == nil, connections[name] == nil {
                resolve(name: name, endpoint: result.endpoint)
            }
        }
        // Gone from the network → gone from the list.
        for name in found.keys where !live.contains(name) { found.removeValue(forKey: name) }
        publish()
    }

    /// Resolve the service to learn the port and the advertised host. The host
    /// is in the service name ("Fused Render (render.fused.local)") because
    /// Network.framework does not expose the SRV target; the connection tells
    /// us the port and confirms reachability.
    private func resolve(name: String, endpoint: NWEndpoint) {
        let conn = NWConnection(to: endpoint, using: .tcp)
        connections[name] = conn
        conn.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            log.info("resolve \(name, privacy: .public): \(String(describing: state), privacy: .public)")
            switch state {
            case .ready:
                var port = 80
                if let path = conn.currentPath, case let .hostPort(_, p)? = path.remoteEndpoint {
                    port = Int(p.rawValue)
                }
                let host = Self.hostFromName(name) ?? "render.fused.local"
                self.found[name] = Server(name: name, host: host, port: port)
                conn.cancel()
                self.connections.removeValue(forKey: name)
                self.publish()
            case .failed, .cancelled:
                self.connections.removeValue(forKey: name)
            default:
                break
            }
        }
        conn.start(queue: .main)
    }

    /// "Fused Render (render.fused.local)" → "render.fused.local".
    static func hostFromName(_ name: String) -> String? {
        guard let open = name.firstIndex(of: "("), let close = name.lastIndex(of: ")"), open < close else { return nil }
        let host = name[name.index(after: open)..<close].trimmingCharacters(in: .whitespaces)
        return host.isEmpty ? nil : host
    }

    private func publish() {
        // Bonjour results and probe hits, merged; prefer the multi-label name —
        // the alias is the same computer.
        let all = (Array(found.values) + Array(probed.values)).sorted { a, b in
            if a.host.contains("fused") != b.host.contains("fused") { return a.host.contains("fused") }
            return a.name < b.name
        }
        var seenHosts = Set<String>()
        var list = all.filter { seenHosts.insert("\($0.host):\($0.port)").inserted }
        if list.contains(where: { $0.host == "render.fused.local" }) {
            list.removeAll { $0.host == "render.local" }
        }
        servers = list
    }
}
