// The widget's configuration intent and the entity it picks — in Shared/ so
// BOTH targets compile them: WidgetKit resolves a configured widget's
// parameters through the app's AppIntents metadata as well as the
// extension's, and an intent that exists only in the extension was shown in
// the edit sheet but never persisted (the tile stayed unconfigured).
import AppIntents
import Foundation
import os

private let log = Logger(subsystem: "io.fused.render", category: "widget-intent")

struct FusedAppEntity: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Fused app"
    static var defaultQuery = FusedAppQuery()

    var id: String
    var label: String
    var host: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(label)", subtitle: "\(host)")
    }

    init(_ app: ManifestApp) {
        id = app.id
        label = app.label
        host = app.host
    }
}

struct FusedAppQuery: EntityStringQuery {
    func entities(matching string: String) async throws -> [FusedAppEntity] {
        let q = string.lowercased()
        return Manifest.load()
            .filter { $0.label.lowercased().contains(q) || $0.name.lowercased().contains(q) }
            .sorted { $0.recency > $1.recency }
            .map(FusedAppEntity.init)
    }

    func entities(for identifiers: [String]) async throws -> [FusedAppEntity] {
        let found = Manifest.load().filter { identifiers.contains($0.id) }.map(FusedAppEntity.init)
        log.info("entities(for: \(identifiers.count)) → \(found.count)")
        return found
    }

    func suggestedEntities() async throws -> [FusedAppEntity] {
        let all = Manifest.load().sorted { $0.recency > $1.recency }.map(FusedAppEntity.init)
        log.info("suggested → \(all.count)")
        return all
    }
}

struct SelectFusedAppIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Fused app"
    static var description = IntentDescription("Which app this tile opens.")

    @Parameter(title: "App")
    var app: FusedAppEntity?

    init() {}

    init(app: FusedAppEntity?) {
        self.app = app
    }
}
