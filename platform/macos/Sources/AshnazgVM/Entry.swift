import Foundation

@main struct AshnazgVMMain {
    static func main() async {
        do {
            let options = try HelperOptions(arguments: Array(CommandLine.arguments.dropFirst()))
            try await runHelper(options)
        } catch {
            FileHandle.standardError.write(Data("ashnazg-vm: \(error.localizedDescription)\n".utf8))
            exit(EXIT_FAILURE)
        }
    }
}
