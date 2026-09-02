// Home Screen widgets: one Fused app per widget. The user adds "Fused app",
// picks which app from the list the phone last saw (Shared/AppManifest.swift,
// refreshed every time the grid loads in the app), and the tile shows that
// app's preview or monogram; tapping it deep-links straight into the app.
// iOS gives one container app one icon — this is how it gets one per app.
import AppIntents
import SwiftUI
import WidgetKit
import os

private let log = Logger(subsystem: "io.fused.render", category: "widget")

@main
struct FusedRenderWidgets: WidgetBundle {
    var body: some Widget {
        FusedAppWidget()
    }
}

// The configuration intent + entity live in Shared/WidgetIntents.swift (both targets).

// MARK: - timeline

struct FusedAppEntry: TimelineEntry {
    let date: Date
    let app: ManifestApp?
    let preview: UIImage?
}

struct FusedAppProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> FusedAppEntry {
        FusedAppEntry(date: .now, app: nil, preview: nil)
    }

    func snapshot(for configuration: SelectFusedAppIntent, in context: Context) async -> FusedAppEntry {
        entry(for: configuration)
    }

    func timeline(for configuration: SelectFusedAppIntent, in context: Context) async -> Timeline<FusedAppEntry> {
        // Static: the app reloads all timelines when the manifest changes.
        Timeline(entries: [entry(for: configuration)], policy: .never)
    }

    private func entry(for configuration: SelectFusedAppIntent) -> FusedAppEntry {
        let apps = Manifest.load()
        let wanted = configuration.app?.id
        // No silent fallback: an unconfigured tile says so, rather than
        // showing the most recent app and masking a selection that did not
        // persist.
        let app = apps.first(where: { $0.id == wanted })
        log.info("entry: wanted=\(wanted ?? "nil", privacy: .public) → \(app?.label ?? "none", privacy: .public) of \(apps.count)")
        let preview = app?.previewURL.flatMap { UIImage(contentsOfFile: $0.path) }
        return FusedAppEntry(date: .now, app: app, preview: preview)
    }
}

// MARK: - the tile

struct FusedAppTile: View {
    @Environment(\.widgetFamily) private var family
    let entry: FusedAppEntry

    var body: some View {
        if let app = entry.app {
            content(app)
                .widgetURL(app.deepLink)
        } else {
            VStack(spacing: 6) {
                Image(systemName: "sparkle").font(.title2)
                Text(Manifest.load().isEmpty
                     ? "Open Fused Render once to list your apps"
                     : "Hold, then Edit Widget to choose an app")
                    .font(.caption2).multilineTextAlignment(.center)
            }
            .foregroundStyle(.secondary)
            .padding()
        }
    }

    @ViewBuilder
    private func content(_ app: ManifestApp) -> some View {
        switch family {
        case .systemMedium:
            HStack(spacing: 12) {
                artwork(app).frame(width: 84, height: 84).clipShape(RoundedRectangle(cornerRadius: 16))
                VStack(alignment: .leading, spacing: 4) {
                    Text(app.label).font(.headline).lineLimit(2)
                    if let tag = app.tag { Text(tag).font(.caption).foregroundStyle(.secondary) }
                    Text(app.host).font(.caption2).foregroundStyle(.tertiary)
                }
                Spacer(minLength: 0)
            }
            .padding(4)
        default:
            ZStack(alignment: .bottomLeading) {
                artwork(app)
                LinearGradient(colors: [.clear, .black.opacity(0.65)], startPoint: .center, endPoint: .bottom)
                Text(app.label)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                    .padding(10)
            }
        }
    }

    @ViewBuilder
    private func artwork(_ app: ManifestApp) -> some View {
        if let preview = entry.preview {
            Image(uiImage: preview).resizable().scaledToFill()
        } else {
            ZStack {
                Monogram.background(app.name)
                Text(Monogram.initial(app.label))
                    .font(.system(size: 44, weight: .semibold, design: .rounded))
                    .foregroundStyle(Monogram.foreground(app.name))
            }
        }
    }
}

struct FusedAppWidget: Widget {
    let kind = "io.fused.render.app"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: SelectFusedAppIntent.self, provider: FusedAppProvider()) { entry in
            FusedAppTile(entry: entry)
                .containerBackground(for: .widget) { Color(red: 27 / 255, green: 29 / 255, blue: 33 / 255) }
        }
        .configurationDisplayName("Fused app")
        .description("Opens one of your Fused apps.")
        .supportedFamilies([.systemSmall, .systemMedium])
        .contentMarginsDisabled()
    }
}
