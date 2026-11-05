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
//     Before saying so, the phone's own /24 is probed for the pinned
//     certificate (`scanSubnet`): a computer with a new DHCP address on a
//     network that filters mDNS is found that way, and the address is
//     remembered as the next fallback.
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
        if denied { return .localNetworkDenied }
        if let found = await scanSubnet(port: server.port, fingerprint: server.fingerprint) {
            return .ok(host: found)
        }
        return .unreachable(host: server.host)
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

    // MARK: the last resort — the phone's own subnet

    /// Neither the name nor the remembered address answered: the computer
    /// may have a new DHCP address on a network that filters mDNS, which is
    /// the one case nothing stored can resolve. Probe the phone's own /24 for
    /// the pinned certificate: a plain TCP connect on the listener's port
    /// first (most addresses refuse or time out at once), then the pinned
    /// HEAD on whatever accepted. Unambiguous and safe because only the
    /// pinned certificate counts, and cheap because it runs only after both
    /// stored hosts failed. Returns the address that answered.
    static func scanSubnet(port: Int, fingerprint: String) async -> String? {
        guard let mine = wifiIPv4() else { return nil }
        let octets = mine.split(separator: ".")
        guard octets.count == 4 else { return nil }
        let prefix = octets[0...2].joined(separator: ".")
        let candidates = (1...254).map { "\(prefix).\($0)" }.filter { $0 != mine }
        log.info("reach: probing \(prefix).0/24 on port \(port)")
        return await withTaskGroup(of: String?.self) { group -> String? in
            var pending = candidates[...]
            func addNext() {
                guard let host = pending.popFirst() else { return }
                group.addTask {
                    guard await tcpOpen(host: host, port: port, timeout: 1.0) else { return nil }
                    return await reach(host: host, port: port, fingerprint: fingerprint, timeout: 3) == .reached ? host : nil
                }
            }
            for _ in 0..<32 { addNext() }
            while let result = await group.next() {
                if let found = result {
                    group.cancelAll()
                    log.info("reach: found the computer at \(found, privacy: .public)")
                    return found
                }
                addNext()
            }
            return nil
        }
    }

    /// The phone's IPv4 on Wi-Fi (`en0`, the first `en*` interface that is up).
    private static func wifiIPv4() -> String? {
        var list: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&list) == 0, let first = list else { return nil }
        defer { freeifaddrs(list) }
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let ifa = cursor {
            defer { cursor = ifa.pointee.ifa_next }
            let flags = Int32(ifa.pointee.ifa_flags)
            guard let sa = ifa.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
                  flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0,
                  String(cString: ifa.pointee.ifa_name).hasPrefix("en") else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                return String(cString: host)
            }
        }
        return nil
    }

    /// Does anything accept a TCP connection on `host:port` within `timeout`?
    private static func tcpOpen(host: String, port: Int, timeout: TimeInterval) async -> Bool {
        guard let nwPort = NWEndpoint.Port(rawValue: UInt16(port)) else { return false }
        return await withCheckedContinuation { cont in
            let connection = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: .tcp)
            let once = Once()
            let finish = { (open: Bool) in
                once.run {
                    connection.cancel()
                    cont.resume(returning: open)
                }
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready: finish(true)
                case .failed, .cancelled: finish(false)
                case .waiting: finish(false)  // no route / refused: not this one
                default: break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { finish(false) }
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
