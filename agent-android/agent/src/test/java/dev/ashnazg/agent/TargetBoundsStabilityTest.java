package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class TargetBoundsStabilityTest {
    @Test public void requiresTwoEqualBoundsObservations() {
        TargetBoundsStability stability = new TargetBoundsStability();
        assertFalse(stability.observe(10, 20, 110, 70, 0));
        assertFalse(stability.observe(10, 20, 110, 70, 119));
        assertTrue(stability.observe(10, 20, 110, 70, 120));
    }

    @Test public void aMovingTargetResetsTheStableObservation() {
        TargetBoundsStability stability = new TargetBoundsStability();
        assertFalse(stability.observe(10, 20, 110, 70, 0));
        assertFalse(stability.observe(10, 40, 110, 90, 100));
        assertFalse(stability.observe(10, 40, 110, 90, 219));
        assertTrue(stability.observe(10, 40, 110, 90, 220));
    }
}
