// iOS has no API for a page or an app to add a Home Screen tile, so the tile
// is a WIDGET (FusedRenderWidgets): one "Fused app" widget per app, each
// configured to open one app. This sheet is the three taps, said once.
import SwiftUI

struct HomeScreenHelp: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label {
                        Text("Long-press an empty spot on the Home Screen, then tap **Edit → Add Widget**.")
                    } icon: { Image(systemName: "1.circle") }
                    Label {
                        Text("Search for **Fused Render** and add the **Fused app** widget.")
                    } icon: { Image(systemName: "2.circle") }
                    Label {
                        Text("Tap the new widget once to choose which app it opens.")
                    } icon: { Image(systemName: "3.circle") }
                } footer: {
                    Text("Each widget opens one app directly. Repeat for as many apps as you like. Long-pressing the Fused Render icon also lists your four most recent apps.")
                }
            }
            .navigationTitle("Apps on the Home Screen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.medium])
    }
}
