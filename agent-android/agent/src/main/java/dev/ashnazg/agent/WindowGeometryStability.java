package dev.ashnazg.agent;

/** Bounded geometry readiness heuristic before injecting a pointer event. */
final class WindowGeometryStability {
    static final long STEADY_WINDOW_MS = 120;
    static final long CHANGED_WINDOW_MS = 500;

    static final class Bounds {
        final int left, top, right, bottom;
        Bounds(int left, int top, int right, int bottom) {
            this.left = left; this.top = top; this.right = right; this.bottom = bottom;
        }
        boolean isEmpty() { return left >= right || top >= bottom; }
        @Override public boolean equals(Object other) {
            if (!(other instanceof Bounds)) return false;
            Bounds that = (Bounds) other;
            return left == that.left && top == that.top && right == that.right && bottom == that.bottom;
        }
        @Override public int hashCode() {
            int result = left; result = result * 31 + top;
            result = result * 31 + right; return result * 31 + bottom;
        }
    }

    private final int previouslyReadyWindowId;
    private final long previouslyReadyGeneration;
    private final Bounds previouslyReadyWindowBounds;
    private boolean windowChanged;
    private Geometry previous;
    private long stableSinceMs;

    WindowGeometryStability(int previouslyReadyWindowId, long previouslyReadyGeneration, Bounds previouslyReadyWindowBounds) {
        this.previouslyReadyWindowId = previouslyReadyWindowId;
        this.previouslyReadyGeneration = previouslyReadyGeneration;
        this.previouslyReadyWindowBounds = previouslyReadyWindowBounds;
    }

    boolean observe(int windowId, long generationBefore, long generationAfter,
                    Bounds nodeScreen, Bounds nodeWindow, Bounds windowScreen,
                    boolean active, boolean focused, long nowMs) {
        if (generationBefore != generationAfter || (!active && !focused) || empty(nodeScreen) || empty(windowScreen)) {
            previous = null;
            return false;
        }
        if (windowId != previouslyReadyWindowId || generationBefore != previouslyReadyGeneration ||
                !windowScreen.equals(previouslyReadyWindowBounds)) windowChanged = true;
        Geometry next = new Geometry(windowId, generationBefore, nodeScreen, nonEmpty(nodeWindow), windowScreen);
        if (!next.equals(previous)) {
            previous = next;
            stableSinceMs = nowMs;
            return false;
        }
        long requiredMs = windowChanged ? CHANGED_WINDOW_MS : STEADY_WINDOW_MS;
        return nowMs - stableSinceMs >= requiredMs;
    }

    private static boolean empty(Bounds bounds) { return bounds == null || bounds.isEmpty(); }
    private static Bounds nonEmpty(Bounds bounds) { return empty(bounds) ? null : bounds; }

    private static final class Geometry {
        final int windowId;
        final long generation;
        final Bounds nodeScreen, nodeWindow, windowScreen;
        Geometry(int windowId, long generation, Bounds nodeScreen, Bounds nodeWindow, Bounds windowScreen) {
            this.windowId = windowId; this.generation = generation;
            this.nodeScreen = nodeScreen; this.nodeWindow = nodeWindow; this.windowScreen = windowScreen;
        }
        @Override public boolean equals(Object other) {
            if (!(other instanceof Geometry)) return false;
            Geometry that = (Geometry) other;
            return windowId == that.windowId && generation == that.generation &&
                    nodeScreen.equals(that.nodeScreen) && same(nodeWindow, that.nodeWindow) &&
                    windowScreen.equals(that.windowScreen);
        }
        @Override public int hashCode() {
            int result = windowId * 31 + Long.hashCode(generation);
            result = result * 31 + nodeScreen.hashCode();
            result = result * 31 + (nodeWindow == null ? 0 : nodeWindow.hashCode());
            return result * 31 + windowScreen.hashCode();
        }
        private static boolean same(Bounds first, Bounds second) {
            return first == null ? second == null : first.equals(second);
        }
    }
}
