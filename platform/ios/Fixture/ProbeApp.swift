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
        }
        .padding(24)
    }
}
