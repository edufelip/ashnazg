package dev.ashnazg.agent;

final class InstrumentationStartup {
    private InstrumentationStartup() {}
    static Failure failure(Throwable error) {
        String message = error == null || error.getMessage() == null ? "instrumentation startup failed" : error.getMessage();
        String lower = message.toLowerCase(java.util.Locale.ROOT);
        if (lower.contains("uiautomation") || lower.contains("automation service"))
            message += "; Android permits only one active UiAutomation service, so stop other UI automation clients and retry";
        return new Failure(1, message);
    }
    static final class Failure {
        final int exitCode;
        final String message;
        Failure(int exitCode, String message) { this.exitCode = exitCode; this.message = message; }
    }
}
