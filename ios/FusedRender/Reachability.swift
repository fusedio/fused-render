// Why the phone cannot reach the computer, told apart so the banner can say
// what to do (docs/LAN_SIMPLIFICATION.md §7). Three causes, three fixes:
//
//   - Local Network access is OFF for this app (the iOS prompt was dismissed,
//     or the switch turned off later in Settings). iOS has no API that says
//     so; the reliable signal is a Bonjour browse for a type the Info.plist
//     lists (`_fusedrender._tcp`, never advertised by anyone) reporting the
//     policy-denied DNS error within a few hundred milliseconds. The browse
//     is also what triggers the system prompt the first time.
//   - The phone is not on Wi-Fi (cellular, airplane mode).
//   - Permission fine, Wi-Fi fine, but nobody answers: a different or
//     isolated network (guest Wi-Fi), the computer asleep, its firewall.
//
// The reach step is the same pinned TLS request the app makes for everything
// else, so a success also says "it is the right computer" and names the host
// (the local name, or the fallback address) the webview should load.
import Foundation
import Network
import os

private let log = Logger(subsystem: "io.fused.render", category: "reach")

enum LanState: Equatable {
    /// The computer answered on `host` with the pinned certificate.
    case ok(host: String)
    case noWifi
    case localNetworkDenied
    /// Permission and Wi-Fi are fine; nothing answered on any host.
    case unreachable(host: String)
}

enum Reachability {
    /// `kDNSServiceErr_PolicyDenied` (dns_sd.h): what a Bonjour browse reports
    /// when Local Network access is off for the app.
    private static let policyDenied: Int32 = -65570
    private static let queue = DispatchQueue(label: "io.fused.render.reach")

    static func diagnose(_ server: Server) async -> LanState {
        let path = await currentPath()
        guard path.status == .satisfied,
              path.usesInterfaceType(.wifi) || path.usesInterfaceType(.wiredEthernet) else {
            return .noWifi
        }
        if await localNetworkDenied(timeout: 1.5) { return .localNetworkDenied }
        var denied = false
        for host in server.candidateHosts {
            switch await reach(host: host, port: server.port, fingerprint: server.fingerprint, timeout: 3) {
            case .reached:
                return .ok(host: host)
            case .denied:
                denied = true
            case .failed:
                continue
            }
        }
        // Some iOS versions fail the connection with "no route to host" at
        // once, before the browser reports anything.
        return denied ? .localNetworkDenied : .unreachable(host: server.host)
    }

    // MARK: the three probes

    private static func currentPath() async -> NWPath {
        await withCheckedContinuation { cont in
            let monitor = NWPathMonitor()
            let once = Once()
            monitor.pathUpdateHandler = { path in
                once.run {
                    monitor.cancel()
                    cont.resume(returning: path)
                }
            }
            monitor.start(queue: queue)
        }
    }

    private static func localNetworkDenied(timeout: TimeInterval) async -> Bool {
        await withCheckedContinuation { cont in
            let params = NWParameters.tcp
            params.includePeerToPeer = true
            let browser = NWBrowser(for: .bonjour(type: "_fusedrender._tcp", domain: nil), using: params)
            let once = Once()
            let finish = { (denied: Bool) in
                once.run {
                    browser.cancel()
                    cont.resume(returning: denied)
                }
            }
            browser.stateUpdateHandler = { state in
                switch state {
                case .waiting(let error), .failed(let error):
                    if case .dns(let code) = error, code == policyDenied {
                        log.info("reach: local network access denied")
                        finish(true)
                    }
                default:
                    break
                }
            }
            browser.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { finish(false) }
        }
    }

    enum ReachResult { case reached, denied, failed }

    /// One pinned HEAD at `https://host:port/`. Any HTTP answer (the grid, or
    /// the 403 how-to-pair page) means reached; a certificate that is not the
    /// pinned one fails the handshake and counts as nobody answering.
    static func reach(host: String, port: Int, fingerprint: String, timeout: TimeInterval) async -> ReachResult {
        var c = URLComponents()
        c.scheme = "https"
        c.host = host
        c.port = port
        c.path = "/"
        guard let url = c.url else { return .failed }
        var req = URLRequest(url: url)
        req.httpMethod = "HEAD"
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue(ShellIdentity.userAgentMarker, forHTTPHeaderField: "User-Agent")
        let session = PinnedSession(fingerprint: fingerprint)
        let started = Date()
        do {
            let (_, response) = try await session.data(for: req)
            return response is HTTPURLResponse ? .reached : .failed
        } catch {
            let quick = Date().timeIntervalSince(started) < 0.5
            if quick && isNoRouteToHost(error) {
                return .denied
            }
            log.info("reach: \(host, privacy: .public):\(port) did not answer: \(String(describing: error), privacy: .public)")
            return .failed
        }
    }

    /// EHOSTUNREACH (65) anywhere in the error's underlying chain: what iOS
    /// returns for a local address when Local Network access is off.
    private static func isNoRouteToHost(_ error: Error) -> Bool {
        var current: NSError? = error as NSError
        while let e = current {
            if e.domain == NSPOSIXErrorDomain && e.code == 65 { return true }
            current = e.userInfo[NSUnderlyingErrorKey] as? NSError
        }
        return false
    }
}

/// Runs its body at most once, from whichever queue calls first.
private final class Once {
    private let lock = NSLock()
    private var done = false

    func run(_ body: () -> Void) {
        lock.lock()
        let first = !done
        done = true
        lock.unlock()
        if first { body() }
    }
}
