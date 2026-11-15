package dev.ashnazg.agent;

import java.util.List;

/** Primitive-only postcondition policy for a single navigation action. */
final class NavigationPostcondition {
    static final int APPLICATION = 1;
    static final int INPUT_METHOD = 2;
    static final long STABLE_MS = 500;

    static final class Bounds {
        final int left, top, right, bottom;
        Bounds(int left, int top, int right, int bottom) {
            this.left=left; this.top=top; this.right=right; this.bottom=bottom;
        }
        boolean isEmpty() { return left >= right || top >= bottom; }
        @Override public boolean equals(Object value) {
            if (!(value instanceof Bounds)) return false;
            Bounds other=(Bounds)value;
            return left==other.left && top==other.top && right==other.right && bottom==other.bottom;
        }
        @Override public int hashCode() { return (((left*31)+top)*31+right)*31+bottom; }
    }
    static final class Window {
        final int id, type;
        final boolean active, focused;
        final Bounds bounds;
        Window(int id, int type, boolean active, boolean focused, Bounds bounds) {
            this.id=id; this.type=type; this.active=active; this.focused=focused; this.bounds=bounds;
        }
        @Override public boolean equals(Object value) {
            if (!(value instanceof Window)) return false;
            Window other=(Window)value;
            return id==other.id && type==other.type && active==other.active && focused==other.focused &&
                    (bounds==null ? other.bounds==null : bounds.equals(other.bounds));
        }
        @Override public int hashCode() {
            int result=id*31+type; result=result*31+(active?1:0); result=result*31+(focused?1:0);
            return result*31+(bounds==null?0:bounds.hashCode());
        }
    }
    static final class State {
        final int rootWindowId;
        final String packageName, semanticFingerprint;
        final List<Window> windows;
        final boolean rootActive, rootFocused, coherent;
        final long generation;
        State(int rootWindowId, String packageName, String semanticFingerprint, List<Window> windows,
              boolean rootActive, boolean rootFocused, long generation, boolean coherent) {
            this.rootWindowId=rootWindowId; this.packageName=packageName; this.semanticFingerprint=semanticFingerprint;
            this.windows=List.copyOf(windows); this.rootActive=rootActive; this.rootFocused=rootFocused;
            this.generation=generation; this.coherent=coherent;
        }
        boolean rootReady() {
            if (rootWindowId < 0 || (!rootActive && !rootFocused)) return false;
            for (Window window : windows) {
                if (window.id == rootWindowId)
                    return window.bounds != null && !window.bounds.isEmpty() && (window.active || window.focused);
            }
            return false;
        }
        boolean sameAs(State other) {
            return other != null && rootWindowId==other.rootWindowId && generation==other.generation &&
                    rootActive==other.rootActive && rootFocused==other.rootFocused &&
                    same(packageName, other.packageName) && same(semanticFingerprint, other.semanticFingerprint) &&
                    windows.equals(other.windows);
        }
        private static boolean same(Object first, Object second) { return first==null ? second==null : first.equals(second); }
    }

    private final State baseline;
    private State lastCandidate;
    private long stableSinceMs;

    NavigationPostcondition(State baseline) { this.baseline=baseline; }

    boolean observe(State candidate, long nowMs, long deadlineMs) {
        if (nowMs >= deadlineMs || baseline == null || candidate == null || !candidate.coherent || !candidate.rootReady() ||
                !isDistinctOutcome(baseline, candidate)) {
            lastCandidate=null;
            return false;
        }
        if (!candidate.sameAs(lastCandidate)) {
            lastCandidate=candidate;
            stableSinceMs=nowMs;
            return false;
        }
        return nowMs-stableSinceMs >= STABLE_MS;
    }

    private static boolean isDistinctOutcome(State before, State after) {
        if (before.rootWindowId != after.rootWindowId || !same(before.packageName, after.packageName) ||
                !same(before.semanticFingerprint, after.semanticFingerprint)) return true;
        for (Window oldWindow : before.windows) {
            if ((oldWindow.type == APPLICATION || oldWindow.type == INPUT_METHOD) && !contains(after.windows, oldWindow.id, oldWindow.type))
                return true;
        }
        return false;
    }

    private static boolean contains(List<Window> windows, int id, int type) {
        for (Window window : windows) if (window.id==id && window.type==type) return true;
        return false;
    }

    private static boolean same(String first, String second) { return first==null ? second==null : first.equals(second); }
}
