// Screens: pick a computer (found on the Wi-Fi, or paired before) → pair by
// QR (or a pasted URL, the simulator's only way) → the grid in a webview.
import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if let server = model.current {
            ConnectedView(server: server)
        } else {
            NavigationStack { ServersView() }
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
    @State private var location: URL?

    var body: some View {
        WebView(url: server.baseURL, pairURL: $model.pendingPairURL) { location = $0 }
            .ignoresSafeArea(edges: .bottom)
            .toolbar(.hidden, for: .navigationBar)
            .overlay(alignment: .bottomTrailing) {
                // A quiet way back to the computer list, parked in the corner
                // under the home indicator where pages keep nothing; the page
                // itself is the chrome.
                Menu {
                    Button("Switch computer", systemImage: "desktopcomputer") { model.disconnect() }
                    if let location { Text(location.host ?? "").font(.footnote) }
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
}
