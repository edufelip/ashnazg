package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class QuietWindowPolicyTest {
    @Test public void preActionIdleCannotSatisfyPostActionQuietWindow() {
        assertEquals(20, QuietWindowPolicy.remainingMs(900, 800, 1000, 120));
        assertEquals(0, QuietWindowPolicy.remainingMs(900, 800, 1020, 120));
    }

    @Test public void accessibilityEventResetsQuietWindowAfterAction() {
        assertEquals(1, QuietWindowPolicy.remainingMs(900, 1100, 1219, 120));
        assertEquals(0, QuietWindowPolicy.remainingMs(900, 1100, 1220, 120));
    }
}
