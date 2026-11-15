package dev.ashnazg.agent;

final class LogSanitizer {
    private LogSanitizer() {}
    static String redact(String line, String secret) {
        return line == null || secret == null || secret.isEmpty() ? line : line.replace(secret, "[REDACTED]");
    }
}
