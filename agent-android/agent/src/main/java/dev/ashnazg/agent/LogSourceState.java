package dev.ashnazg.agent;

final class LogSourceState {
    private volatile String failure = "logcat source has not started";
    private volatile boolean closing;
    void started() { if (!closing) failure = null; }
    void ended() { if (!closing) failure = "logcat stream ended unexpectedly"; }
    void failed(Throwable cause) {
        if (!closing) failure = "logcat reader failed: " + (cause == null ? "unknown error" : cause.getClass().getSimpleName());
    }
    void close() { closing = true; }
    String failure() { return closing ? null : failure; }
}
