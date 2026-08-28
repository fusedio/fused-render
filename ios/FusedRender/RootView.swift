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

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if ScannerView.isSupported {
                    ScannerView { code in accept(code) }
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
                    Button("Pair") { accept(pasted) }
                        .buttonStyle(.borderedProminent)
                        .disabled(pasted.trimmingCharacters(in: .whitespaces).isEmpty)
                }
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

    private func accept(_ text: String) {
        guard let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              model.pair(with: url) else {
            problem = "That is not a Fused pairing code."
            return
        }
        dismiss()
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

    var body: some View {
        WebView(url: server.baseURL, pairURL: $model.pendingPairURL, controller: web)
            .ignoresSafeArea(edges: .bottom)
            .toolbar(.hidden, for: .navigationBar)
            .sheet(isPresented: $showHomeScreenHelp) { HomeScreenHelp() }
            // Two kinds of chrome, never both. On the grid: a proper bottom
            // bar — the listing is ours, so it gets real controls. Inside an
            // app: one floating button in the corner under the home indicator,
            // because the page owns the screen (owner's call).
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if onGrid { gridBar }
            }
            .overlay(alignment: .bottomTrailing) {
                if !onGrid { appMenu }
            }
            .animation(.easeInOut(duration: 0.18), value: onGrid)
    }

    private var gridBar: some View {
        HStack(spacing: 0) {
            BarButton(title: "Computers", systemImage: "desktopcomputer") { model.disconnect() }
            BarButton(title: "Refresh", systemImage: "arrow.clockwise") { web.load(server.baseURL) }
            BarButton(title: "Home Screen", systemImage: "plus.square.on.square") { showHomeScreenHelp = true }
        }
        .padding(.top, 6)
        .frame(maxWidth: .infinity)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    private var appMenu: some View {
        Menu {
            Button("All apps", systemImage: "square.grid.2x2") { web.load(server.baseURL) }
            if web.canGoBack {
                Button("Back", systemImage: "chevron.left") { web.goBack() }
            }
            Button("Reload", systemImage: "arrow.clockwise") { web.webView?.reload() }
            Divider()
            Button("Add an app to the Home Screen", systemImage: "plus.square.on.square") { showHomeScreenHelp = true }
            Button("Switch computer", systemImage: "desktopcomputer") { model.disconnect() }
            Text(server.host).font(.footnote)
        } label: {
            Image(systemName: "ellipsis.circle.fill")
                .font(.title2)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
                .padding(8)
                .background(.ultraThinMaterial, in: Circle())
                .padding(.trailing, 12)
                .padding(.bottom, 6)
        }
    }
}

/// A tab-bar-shaped button: icon over a small label, equal width.
struct BarButton: View {
    let title: String
    let systemImage: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                Image(systemName: systemImage).font(.system(size: 20, weight: .regular))
                Text(title).font(.system(size: 10.5, weight: .medium))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
    }
}
