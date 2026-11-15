package dev.ashnazg.agent;

/** Admission policy for one semantic-target reacquisition before pointer input begins. */
final class SemanticPreInputRecovery {
    private static final int MAX_REACQUISITIONS = 1;

    private SemanticPreInputRecovery() {}

    static boolean canBeginReacquisition(SelectorResolver.Target target, int reacquisitions,
                                         long nowMs, long deadlineMs) {
        boolean semanticSelector = target != null && target.id == null && target.snapshotId == null &&
                (target.testId != null || target.text != null);
        return semanticSelector && reacquisitions < MAX_REACQUISITIONS && beforeDeadline(nowMs, deadlineMs);
    }

    static boolean beforeDeadline(long nowMs, long deadlineMs) { return nowMs < deadlineMs; }

    static boolean sameIdentityAndPackage(SemanticIdentity originalIdentity, String originalPackage,
                                          SemanticIdentity candidateIdentity, String candidatePackage) {
        return originalIdentity != null && originalIdentity.matches(candidateIdentity) &&
                originalPackage != null && !originalPackage.isEmpty() && originalPackage.equals(candidatePackage);
    }
}
