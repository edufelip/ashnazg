package dev.ashnazg.agent;

final class TargetBoundsStability {
    private int left, top, right, bottom;
    private boolean initialized;
    private long stableSinceMs;

    boolean observe(int nextLeft, int nextTop, int nextRight, int nextBottom, long nowMs) {
        if (!initialized || left != nextLeft || top != nextTop || right != nextRight || bottom != nextBottom) {
            left = nextLeft; top = nextTop; right = nextRight; bottom = nextBottom;
            initialized = true;
            stableSinceMs = nowMs;
            return false;
        }
        return nowMs - stableSinceMs >= 120;
    }
}
