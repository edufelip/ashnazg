package dev.ashnazg.agent;

/** Pure deadline policy for waiting on one already-refreshed target window ID. */
final class TargetWindowWaitPolicy {
    enum Decision { WAIT, READY, EXPIRED }

    private Integer targetWindowId;
    private final long deadlineMs;

    TargetWindowWaitPolicy(long deadlineMs) {
        this.deadlineMs = deadlineMs;
    }

    TargetWindowWaitPolicy(int targetWindowId, long deadlineMs) {
        this(deadlineMs);
        this.targetWindowId = targetWindowId;
    }

    int validateRefreshedWindowId(int windowId) {
        if (targetWindowId == null) targetWindowId = windowId;
        if (windowId != targetWindowId)
            throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE,
                    "target accessibility window changed while preparing input");
        return targetWindowId;
    }

    Decision observe(Integer visibleWindowId, long nowMs) {
        if (nowMs >= deadlineMs) return Decision.EXPIRED;
        if (visibleWindowId != null && visibleWindowId.equals(targetWindowId)) return Decision.READY;
        return Decision.WAIT;
    }
}
