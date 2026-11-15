package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class SemanticPreInputRecoveryTest {
    private static final SemanticIdentity INCREMENT = new SemanticIdentity("dev.ashnazg.fixture:id/increment", "Count: 0", "button", false);

    @Test public void semanticInvalidationCanReacquireSameIdentityInSamePackageBeforeInput() {
        assertTrue(SemanticPreInputRecovery.canBeginReacquisition(semanticTarget(), 0, 100, 2100));
        assertTrue(SemanticPreInputRecovery.sameIdentityAndPackage(
                INCREMENT, "dev.ashnazg.fixture", INCREMENT, "dev.ashnazg.fixture"));
    }

    @Test public void numericAndSnapshotSelectorsNeverRebind() {
        assertFalse(SemanticPreInputRecovery.canBeginReacquisition(
                new SelectorResolver.Target(null, null, 4, 7), 0, 100, 2100));
        assertFalse(SemanticPreInputRecovery.canBeginReacquisition(
                new SelectorResolver.Target(null, null, 4, null), 0, 100, 2100));
    }

    @Test public void changedIdentityOrPackageFailsClosed() {
        assertFalse(SemanticPreInputRecovery.sameIdentityAndPackage(
                INCREMENT, "dev.ashnazg.fixture",
                new SemanticIdentity("dev.ashnazg.fixture:id/increment", "Count: 1", "button", false), "dev.ashnazg.fixture"));
        assertFalse(SemanticPreInputRecovery.sameIdentityAndPackage(
                INCREMENT, "dev.ashnazg.fixture", INCREMENT, "com.other.app"));
    }

    @Test public void unknownPackageCannotAuthorizeReacquisition() {
        assertFalse(SemanticPreInputRecovery.sameIdentityAndPackage(INCREMENT, null, INCREMENT, null));
        assertFalse(SemanticPreInputRecovery.sameIdentityAndPackage(INCREMENT, "", INCREMENT, ""));
    }

    @Test public void recoveryIsBoundedBySharedDeadlineAndAttemptBudget() {
        assertFalse(SemanticPreInputRecovery.canBeginReacquisition(semanticTarget(), 0, 2100, 2100));
        assertFalse(SemanticPreInputRecovery.canBeginReacquisition(semanticTarget(), 1, 100, 2100));
    }

    @Test public void preparedCoordinatesNeedRemainingTimeBeforeInput() {
        assertTrue(SemanticPreInputRecovery.beforeDeadline(2099, 2100));
        assertFalse(SemanticPreInputRecovery.beforeDeadline(2100, 2100));
        assertFalse(SemanticPreInputRecovery.beforeDeadline(2101, 2100));
    }

    private static SelectorResolver.Target semanticTarget() {
        return new SelectorResolver.Target("dev.ashnazg.fixture:id/increment", null, null, null);
    }
}
