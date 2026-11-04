// Screens: pick a computer (paired before) or pair one by QR (or a pasted URL,
// the simulator's only way) → the grid, the bots pages and the apps in a
// webview, with a banner above it saying why the computer cannot be reached
// when it cannot (Reachability.swift).
//
// Bots on the phone: `/bots` (the list) and `/bots/<id>` (one bot's chat) are
// web pages the paired computer's LAN listener serves from the same lan entry
// as the grid; this file only gives them a title and a way back. Deep links
// `fusedrender://open?host=…&page=bots` / `&bot=<id>` open them (AppModel),
// and the first Home Screen quick action is "Bots" (ManifestSync). Photos for
// a bot ride the page's own <input type=file>, i.e. the webview's picker.
import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            if let server = model.current {
                ConnectedView(server: server)
                    .id(server.fingerprint)
            } else {
                NavigationStack { ServersView() }
            }
        }
        // Widgets open fusedrender://open?… ; quick actions post the same URL
        // through SceneDelegate.
        .onOpenURL { url in _ = model.open(deepLink: url) }
        .onReceive(NotificationCenter.default.publisher(for: SceneDelegate.openNotification)) { note in
            if let url = note.object as? URL { _ = model.open(deepLink: url) }
        }
        .onAppear {
            // A quick action that cold-launched the app arrived before any
            // view could listen for the notification.
            if let url = SceneDelegate.pending {
                SceneDelegate.pending = nil
                _ = model.open(deepLink: url)
            }
        }
    }
}

// MARK: - Pick a computer

struct ServersView: View {
    @EnvironmentObject var model: AppModel
    @State private var pairing = false

    var body: some View {
        List {
            if !model.known.isEmpty {
                Section("Paired before") {
                    ForEach(model.known) { server in
                        Button { model.open(server) } label: { ServerRow(server: server) }
                            .swipeActions { Button("Forget", role: .destructive) { model.forget(server) } }
                    }
                }
            }
            Section {
                Button { pairing = true } label: {
                    Label("Pair a computer", systemImage: "qrcode.viewfinder")
                }
            } footer: {
                Text("On the computer, turn on Preferences → Phone access and scan the code it shows. Pairing is once per computer; it keeps working when the Wi-Fi or the computer's address changes.")
            }
        }
        .navigationTitle("Fused Render")
        .sheet(isPresented: $pairing) {
            PairView()
        }
    }
}

struct ServerRow: View {
    let server: Server
    var body: some View {
        HStack {
            Image(systemName: "desktopcomputer")
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(server.name).font(.body.weight(.medium))
                Text(server.host)
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }
}

// MARK: - Pair

struct PairView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var pasted = ""
    @State private var problem: String?
    /// Bumped after a failed pairing so a fresh ScannerView starts scanning again.
    @State private var scanRun = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if ScannerView.isSupported {
                    ScannerView { code in accept(code) }
                        .id(scanRun)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.quaternary))
                        .frame(maxHeight: 360)
                    Text("On the computer, open **Preferences → Phone access** and point the camera at the QR code.")
                        .font(.callout).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                } else {
                    ContentUnavailableView("No camera here",
                                           systemImage: "camera.metering.unknown",
                                           description: Text("Paste the pairing link from Preferences → Phone access instead."))
                        .frame(maxHeight: 220)
                }
                HStack {
                    TextField("https://…/pair?t=…", text: $pasted)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .textFieldStyle(.roundedBorder)
                    Button("Pair") { _ = accept(pasted) }
                        .buttonStyle(.borderedProminent)
                        .disabled(pasted.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if let problem {
                    Text(problem).font(.footnote).foregroundStyle(.red)
                }
                Spacer()
            }
            .padding()
            .navigationTitle("Pair a computer")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }

    /// Returns whether the text looked like a pairing code (the scanner keeps
    /// scanning past codes that are not). Nothing is fetched here: the code
    /// carries everything, and the /pair load itself is the first contact.
    @discardableResult
    private func accept(_ text: String) -> Bool {
        guard AppModel.looksLikePairingCode(text),
              let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            problem = "That is not a Fused pairing code."
            return false
        }
        problem = nil
        if model.pair(with: url) {
            dismiss()
        } else {
            problem = model.pairProblem ?? "Pairing failed."
            scanRun += 1  // let the camera try again
        }
        return true
    }
}

// MARK: - Connected: the grid, the bots and the apps

struct ConnectedView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    let server: Server
    @StateObject private var web = WebController()
    @State private var showHomeScreenHelp = false
    @State private var showPair = false
    /// The host the webview reaches the computer on: the name until the
    /// probe finds the computer only on its address.
    @State private var host: String
    @State private var lanState: LanState?
    @State private var diagnosing = false
    @State private var diagnoseRun = 0

    init(server: Server) {
        self.server = server
        _host = State(initialValue: server.host)
    }

    /// Where the webview is, as far as the top bar cares.
    private enum Place {
        /// The grid itself (or the pairing pages around it) — nothing to go back to.
        case grid
        /// `/bots`, the bots list.
        case bots
        /// `/bots/<id>`, one bot's chat.
        case bot
        /// Anything else: an app page.
        case app
    }

    private var place: Place {
        guard let loc = web.location else { return .grid }
        let path = loc.path
        if path == "/" || path == "/index.html" || path == "/pair" { return .grid }
        if path == "/bots" || path == "/bots/" { return .bots }
        if path.hasPrefix("/bots/") { return .bot }
        return .app
    }

    private var baseURL: URL { server.url(host: host) }
    private var botsURL: URL { baseURL.appendingPathComponent("bots") }

    /// The page's own <title> once it lands; until then (or for a page without
    /// one) the app's name from the manifest, else its folder name.
    private var appTitle: String {
        if !web.title.isEmpty { return web.title }
        guard let loc = web.location,
              let entry = URLComponents(url: loc, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "path" })?.value else { return "…" }
        let folder = (entry as NSString).deletingLastPathComponent
        if let app = Manifest.load().first(where: { $0.path == folder }) { return app.label }
        return (folder as NSString).lastPathComponent
    }

    private var title: String {
        switch place {
        case .grid: return "Apps"
        case .bots: return "Bots"
        case .bot: return web.title.isEmpty ? "Bot" : web.title  // the page sets it to the bot's name
        case .app: return appTitle
        }
    }

    var body: some View {
        WebView(server: server, host: host, pairURL: $model.pendingPairURL, controller: web,
                onGridLoaded: { landed in
                    model.confirmPairing(host: landed)
                    model.noteReached(server, host: landed)
                    lanState = .ok(host: landed)
                },
                onNavigationFailed: { diagnoseRun += 1 })
            .ignoresSafeArea(edges: .bottom)
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showHomeScreenHelp) { HomeScreenHelp() }
            .sheet(isPresented: $showPair) { PairView() }
            // One top bar, on the grid, the bots pages and inside apps alike
            // (owner's call — no floating button). Left: the computer (grid),
            // the bots list (a bot's chat) or home (bots list, app). Middle:
            // where you are. Right: the few actions. Under it, the reason the
            // computer cannot be reached, when it cannot.
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    topBar
                    if let state = lanState, state != .ok(host: host) {
                        ReachabilityBanner(state: state, server: server, retry: { diagnoseRun += 1 },
                                           scan: { showPair = true })
                    }
                }
            }
            // The diagnosis runs on appear, on every return to the foreground
            // and after every navigation failure (the webview asks for it).
            .task(id: diagnoseRun) { await diagnose() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { diagnoseRun += 1 }
            }
    }

    @MainActor
    private func diagnose() async {
        guard !diagnosing else { return }
        diagnosing = true
        defer { diagnosing = false }
        let state = await Reachability.diagnose(server)
        lanState = state
        if case .ok(let reached) = state {
            model.noteReached(server, host: reached)
            if reached != host {
                // Found only on the other host: move the webview there, and
                // re-send a pairing that never got through on the name.
                host = reached
                model.retryPairing(on: reached)
            } else if web.location == nil {
                web.load(baseURL)
            }
        }
    }

    @ViewBuilder
    private var leftButton: some View {
        switch place {
        case .grid:
            Button { model.disconnect() } label: {
                Label("Computers", systemImage: "desktopcomputer").labelStyle(.iconOnly)
            }
            .accessibilityLabel("Switch computer")
        case .bot:
            Button { web.load(botsURL) } label: {
                Label("Bots", systemImage: "chevron.left").labelStyle(.iconOnly)
            }
            .accessibilityLabel("Bots")
        case .bots, .app:
            Button { web.load(baseURL) } label: {
                Label("All apps", systemImage: "house").labelStyle(.iconOnly)
            }
            .accessibilityLabel("All apps")
        }
    }

    private var topBar: some View {
        HStack(spacing: 8) {
            leftButton
            VStack(spacing: 0) {
                Text(title)
                    .font(.headline)
                    .lineLimit(1)
                Text(server.name)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            Menu {
                Button("Reload", systemImage: "arrow.clockwise") { web.webView?.reload() }
                Button("Bots", systemImage: "sparkles") { web.load(botsURL) }
                Button("Add an app to the Home Screen", systemImage: "plus.square.on.square") { showHomeScreenHelp = true }
                Divider()
                Button("Switch computer", systemImage: "desktopcomputer") { model.disconnect() }
                Text(baseURL.absoluteString).font(.footnote)
            } label: {
                Label("More", systemImage: "ellipsis.circle").labelStyle(.iconOnly)
            }
        }
        .font(.title3)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }
}

// MARK: - Why the computer cannot be reached

struct ReachabilityBanner: View {
    let state: LanState
    let server: Server
    let retry: () -> Void
    let scan: () -> Void

    private var message: String {
        switch state {
        case .ok:
            return ""
        case .localNetworkDenied:
            return "Fused Render needs Local Network access to reach your computer. It is turned off for this app."
        case .noWifi:
            return "Connect this phone to the same Wi-Fi as your computer."
        case .unreachable:
            return "Can't reach \(server.name) on this Wi-Fi. Make sure both are on the same network — guest and hotel networks often keep devices from seeing each other. If the computer is asleep, wake it."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(message, systemImage: state == .noWifi ? "wifi.slash" : "exclamationmark.triangle")
                .font(.footnote)
            HStack(spacing: 12) {
                if state == .localNetworkDenied {
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    }
                }
                Button("Try again", action: retry)
                if case .unreachable = state {
                    Button("Scan a new code", action: scan)
                }
            }
            .font(.footnote.weight(.semibold))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.yellow.opacity(0.18))
        .overlay(alignment: .bottom) { Divider() }
    }
}
