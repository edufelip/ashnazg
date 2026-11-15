package dev.ashnazg.agent;

import java.util.ArrayList;
import java.util.List;

final class SelectorResolver {
    enum Failure { INVALID_SELECTOR, STALE, NOT_FOUND, AMBIGUOUS, DISABLED }
    static final class SelectorException extends RuntimeException {
        final Failure failure;
        SelectorException(Failure failure, String message) { super(message); this.failure = failure; }
    }
    static final class Node {
        final int id;
        final String testId;
        final String text;
        final boolean enabled;
        Node(int id, String testId, String text, boolean enabled) {
            this.id = id; this.testId = emptyToNull(testId); this.text = emptyToNull(text); this.enabled = enabled;
        }
    }
    static final class Target {
        final String testId;
        final String text;
        final Integer id;
        final Integer snapshotId;
        Target(String testId, String text, Integer id, Integer snapshotId) {
            this.testId = emptyToNull(testId); this.text = emptyToNull(text); this.id = id; this.snapshotId = snapshotId;
        }
    }

    static Node resolve(List<Node> nodes, Target target, int currentSnapshotId) {
        if (target == null) throw invalid();
        int selectorCount = (target.testId != null ? 1 : 0) + (target.text != null ? 1 : 0) + (target.id != null ? 1 : 0);
        if (selectorCount != 1 || (target.id != null && target.snapshotId == null) ||
                (target.id == null && target.snapshotId != null)) throw invalid();
        if (target.id != null && target.snapshotId == null) throw invalid();
        if (target.id != null && target.snapshotId != currentSnapshotId) {
            throw new SelectorException(Failure.STALE, "stale target snapshotId");
        }
        List<Node> matches = new ArrayList<>();
        for (Node node : nodes) {
            boolean match = target.id != null ? node.id == target.id :
                    target.testId != null ? matchesTestId(target.testId, node.testId) : target.text.equals(node.text);
            if (match) matches.add(node);
        }
        if (matches.isEmpty()) throw new SelectorException(Failure.NOT_FOUND, "selector did not match a node");
        if (matches.size() != 1) throw new SelectorException(Failure.AMBIGUOUS, "ambiguous: selector matched multiple nodes");
        if (!matches.get(0).enabled) throw new SelectorException(Failure.DISABLED, "target node is disabled");
        return matches.get(0);
    }

    static String normalizeId(String id) {
        if (id == null) return null;
        int colon = id.lastIndexOf(':');
        int slash = id.lastIndexOf('/');
        return id.substring(Math.max(colon, slash) + 1);
    }
    private static boolean matchesTestId(String selector, String nodeTestId) {
        if (nodeTestId == null) return false;
        if (selector.contains(":id/")) return selector.equals(nodeTestId);
        return selector.equals(normalizeId(nodeTestId));
    }
    private static SelectorException invalid() { return new SelectorException(Failure.INVALID_SELECTOR, "exactly one selector is required; numeric IDs require the matching snapshotId"); }
    private static String emptyToNull(String value) { return value == null || value.isEmpty() ? null : value; }
}
