package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class TargetWindowWaitPolicyTest {
    @Test public void missingTargetCanAppearBeforeTheOriginalDeadline() {
        TargetWindowWaitPolicy policy = new TargetWindowWaitPolicy(12, 2000);

        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(null, 0));
        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(null, 16));
        assertEquals(TargetWindowWaitPolicy.Decision.READY, policy.observe(12, 32));
    }

    @Test public void permanentlyMissingTargetExpiresAtTheOriginalDeadline() {
        TargetWindowWaitPolicy policy = new TargetWindowWaitPolicy(12, 2000);

        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(null, 0));
        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(null, 1984));
        assertEquals(TargetWindowWaitPolicy.Decision.EXPIRED, policy.observe(null, 2000));
    }

    @Test public void anotherWindowIdNeverSatisfiesTheTargetLookup() {
        TargetWindowWaitPolicy policy = new TargetWindowWaitPolicy(12, 2000);

        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(5, 16));
        assertEquals(TargetWindowWaitPolicy.Decision.READY, policy.observe(12, 32));
    }

    @Test public void aLateTargetDoesNotExtendTheDeadline() {
        TargetWindowWaitPolicy policy = new TargetWindowWaitPolicy(12, 2000);

        assertEquals(TargetWindowWaitPolicy.Decision.EXPIRED, policy.observe(12, 2000));
        assertEquals(TargetWindowWaitPolicy.Decision.EXPIRED, policy.observe(12, 2001));
    }
    @Test public void refreshedTargetCannotChangeWindowsThroughSemanticRecovery() {
        TargetWindowWaitPolicy policy = new TargetWindowWaitPolicy(2000);
        assertEquals(12, policy.validateRefreshedWindowId(12));
        assertEquals(TargetWindowWaitPolicy.Decision.WAIT, policy.observe(null, 16));
        // The same policy survives a permitted outer semantic node reacquisition.
        try {
            policy.validateRefreshedWindowId(13);
            fail("changed target window must be rejected");
        } catch (SelectorResolver.SelectorException failure) {
            assertEquals(SelectorResolver.Failure.STALE, failure.failure);
            // preparePointerInput only permits semantic reacquisition for this marker.
            assertFalse(failure.preInputInvalidation);
        }
        policy.validateRefreshedWindowId(12);
    }
}
