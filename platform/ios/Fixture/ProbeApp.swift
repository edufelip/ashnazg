import SwiftUI

@main
struct ProbeApp: App {
    var body: some Scene {
        WindowGroup { ProbeView() }
    }
}

private struct ProbeView: View {
    @State private var count = 0
    @State private var input = ""
    @State private var secret = ""

    var body: some View {
        VStack(spacing: 24) {
            Text("Count: \(count)")
                .font(.title)
                .accessibilityIdentifier("fixture-count")
            Button("Increment") { count += 1 }
                .accessibilityIdentifier("fixture-increment")
            TextField("Probe text", text: $input)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("fixture-input")
            // Synthetic secure field: its value must never be serialized into
            // any snapshot; the bridge types into it but only redaction is
            // observable.
            SecureField("Fixture secret", text: $secret)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("fixture-secret")
        }
        .padding(24)
    }
}
