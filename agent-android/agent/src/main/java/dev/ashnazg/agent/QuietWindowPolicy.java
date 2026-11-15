package dev.ashnazg.agent;

final class QuietWindowPolicy {
    private QuietWindowPolicy() {}
    static long remainingMs(long actionCompletedAtMs, long lastEventAtMs, long nowMs, long quietMs) {
        long quietStart = Math.max(actionCompletedAtMs, lastEventAtMs);
        return Math.max(0, quietMs - Math.max(0, nowMs - quietStart));
    }
}
