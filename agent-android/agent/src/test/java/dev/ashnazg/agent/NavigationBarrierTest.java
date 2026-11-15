package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class NavigationBarrierTest {
    @Test public void navigationSettlesOnlyAfterObservedOutcomeAndQuietWindow() {
        assertTrue(NavigationBarrier.settled(true, true));
        assertFalse(NavigationBarrier.settled(false, true));
        assertFalse(NavigationBarrier.settled(true, false));
    }

    @Test public void mutationDoesNotSettleUntilContentEventAndQuietWindow() {
        assertTrue(NavigationBarrier.afterMutation(true, true));
        assertFalse(NavigationBarrier.afterMutation(false, true));
        assertFalse(NavigationBarrier.afterMutation(true, false));
    }
}
