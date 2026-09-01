// Screens: pick a computer (found on the Wi-Fi, or paired before) → pair by
// QR (or a pasted URL, the simulator's only way) → the grid in a webview.
import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            if let server = model.current {
                ConnectedView(server: server)
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
    @State private var pairing: Server?

    var body: some View {
        List {
            if !model.known.isEmpty {
                Section("Paired before") {
                    ForEach(model.known) { server in
                        Button { model.open(server) } label: { ServerRow(server: server, live: model.discovered.contains(server)) }
                            .swipeActions { Button("Forget", role: .destructive) { model.forget(server) } }
                    }
                }
            }
            Section {
                let fresh = model.discovered.filter { !model.known.contains($0) }
                if fresh.isEmpty {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text(model.known.isEmpty ? "Looking for a computer running Fused Render…" : "Looking for other computers…")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    ForEach(fresh) { server in
                        Button { pairing = server } label: { ServerRow(server: server, live: true) }
                    }
                }
            } header: {
                Text("On this Wi-Fi")
            } footer: {
                Text("On the computer, turn on Preferences → Render local network. New computers appear here; tap one to pair.")
            }
        }
        .navigationTitle("Fused Render")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { pairing = Server(name: "Fused Render", host: "render.fused.local", port: 80) } label: {
                    Label("Pair", systemImage: "qrcode.viewfinder")
                }
            }
        }
        .sheet(item: $pairing) { server in
            PairView(server: server)
        }
    }
}

struct ServerRow: View {
    let server: Server
    let live: Bool
    var body: some View {
        HStack {
            Image(systemName: "desktopcomputer")
                .foregroundStyle(live ? Color.accentColor : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text(server.host).font(.body.weight(.medium))
                Text(live ? "Online" : "Not seen right now")
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
    let server: Server
    @State private var pasted = ""
    @State private var problem: String?
    @State private var busy = false
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
                    Text("On the computer, open **Preferences → Render local network** and point the camera at the QR code.")
                        .font(.callout).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                } else {
                    ContentUnavailableView("No camera here",
                                           systemImage: "camera.metering.unknown",
                                           description: Text("Paste the pairing link from Preferences → Render local network instead."))
                        .frame(maxHeight: 220)
                }
                HStack {
                    TextField("http://render.fused.local/pair?t=…", text: $pasted)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .textFieldStyle(.roundedBorder)
                    Button("Pair") { _ = accept(pasted) }
                        .buttonStyle(.borderedProminent)
                        .disabled(busy || pasted.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if busy { ProgressView() }
                if let problem {
                    Text(problem).font(.footnote).foregroundStyle(.red)
                }
                Spacer()
            }
            .padding()
            .navigationTitle("Pair with \(server.host)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }

    /// Returns whether the text looked like a pairing code (the scanner keeps
    /// scanning past codes that are not). The sheet stays up until the pairing
    /// settles, so a certificate mismatch is an error the user actually sees.
    @discardableResult
    private func accept(_ text: String) -> Bool {
        guard AppModel.looksLikePairingCode(text),
              let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            problem = "That is not a Fused pairing code."
            return false
        }
        problem = nil
        busy = true
        Task {
            let ok = await model.pair(with: url)
            busy = false
            if ok {
                dismiss()
            } else {
                problem = model.pairProblem ?? "Pairing failed."
                scanRun += 1  // let the camera try again
            }
        }
        return true
    }
}

// MARK: - Connected: the grid and the apps

struct ConnectedView: View {
    @EnvironmentObject var model: AppModel
    let server: Server
    @StateObject private var web = WebController()
    @State private var showHomeScreenHelp = false

    /// On the grid itself (or the pairing pages around it) — nothing to go back to.
    private var onGrid: Bool {
        guard let loc = web.location else { return true }
        return loc.path == "/" || loc.path == "/index.html" || loc.path == "/pair"
    }

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

    var body: some View {
        WebView(server: server, pairURL: $model.pendingPairURL, controller: web)
            .ignoresSafeArea(edges: .bottom)
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showHomeScreenHelp) { HomeScreenHelp() }
            // One top bar, on the grid and inside apps alike (owner's call —
            // no floating button, no back). Left: the computer (grid) or home
            // (app). Middle: where you are. Right: the few actions.
            .safeAreaInset(edge: .top, spacing: 0) { topBar }
    }

    private var topBar: some View {
        HStack(spacing: 8) {
            if onGrid {
                Button { model.disconnect() } label: {
                    Label("Computers", systemImage: "desktopcomputer").labelStyle(.iconOnly)
                }
                .accessibilityLabel("Switch computer")
            } else {
                Button { web.load(server.baseURL) } label: {
                    Label("All apps", systemImage: "house").labelStyle(.iconOnly)
                }
                .accessibilityLabel("All apps")
            }
            VStack(spacing: 0) {
                Text(onGrid ? "Apps" : appTitle)
                    .font(.headline)
                    .lineLimit(1)
                Text(server.host)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            Menu {
                Button("Reload", systemImage: "arrow.clockwise") { web.webView?.reload() }
                Button("Add an app to the Home Screen", systemImage: "plus.square.on.square") { showHomeScreenHelp = true }
                Divider()
                Button("Switch computer", systemImage: "desktopcomputer") { model.disconnect() }
                Text(server.baseURL.absoluteString).font(.footnote)
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
