package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class WindowGeometryStabilityTest {
    private static final WindowGeometryStability.Bounds NODE_SCREEN = bounds(40, 200, 440, 320);
    private static final WindowGeometryStability.Bounds NODE_WINDOW = bounds(40, 80, 440, 200);
    private static final WindowGeometryStability.Bounds WINDOW_SCREEN = bounds(0, 120, 1080, 1920);

    @Test public void newWindowDoesNotPassOnATransient120msPlateau() {
        WindowGeometryStability stability = new WindowGeometryStability(-1, -1, null);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 120));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 250));

        WindowGeometryStability.Bounds moved = bounds(0, 173, 1080, 1973);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 260));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 759));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 760));
    }

    @Test public void windowFrameAndEventEpochChangesResetStability() {
        WindowGeometryStability stability = new WindowGeometryStability(-1, -1, null);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 300));

        WindowGeometryStability.Bounds movedFrame = bounds(0, 173, 1080, 1973);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 301));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 700));
        assertFalse(stability.observe(7, 5, 6, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 701));
        assertFalse(observe(stability, 7, 6, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 702));
        assertFalse(observe(stability, 7, 6, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 1201));
        assertTrue(observe(stability, 7, 6, NODE_SCREEN, NODE_WINDOW, movedFrame, true, true, 1202));
    }

    @Test public void alreadyReadyWindowUses120msForSteadyActions() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 119));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 120));
    }

    @Test public void windowFrameChangeWithoutEventNeeds500msEvenAfterReturningToPriorFrame() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        WindowGeometryStability.Bounds moved = bounds(0, 173, 1080, 1973);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 10));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 130));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 140));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 260));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 640));
    }

    @Test public void firstSampleOfResizedReadyWindowNeeds500msWithoutAnEvent() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        WindowGeometryStability.Bounds moved = bounds(0, 173, 1080, 1973);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 0));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 120));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, moved, true, true, 500));
    }

    @Test public void sameWindowWithNewEventGenerationUses500ms() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        assertFalse(observe(stability, 7, 5, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        assertFalse(observe(stability, 7, 5, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 120));
        assertFalse(observe(stability, 7, 5, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 499));
        assertTrue(observe(stability, 7, 5, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 500));
    }

    @Test public void windowMustBeActiveOrFocused() {
        WindowGeometryStability stability = new WindowGeometryStability(-1, -1, null);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, false, false, 0));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, false, false, 100));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, false, 200));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, false, 699));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, false, 700));
    }

    @Test public void emptyNodeWindowBoundsAreOptionalForCompose() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, null, WINDOW_SCREEN, true, true, 0));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, null, WINDOW_SCREEN, true, true, 120));
    }

    @Test public void missingWindowGapResetsTheGeometryStabilityInterval() {
        WindowGeometryStability stability = new WindowGeometryStability(7, 4, WINDOW_SCREEN);
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 0));
        stability.resetAfterMissingWindow();

        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 120));
        assertFalse(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 239));
        assertTrue(observe(stability, 7, 4, NODE_SCREEN, NODE_WINDOW, WINDOW_SCREEN, true, true, 240));
    }

    private static boolean observe(WindowGeometryStability stability, int id, long epoch,
                                   WindowGeometryStability.Bounds screen,
                                   WindowGeometryStability.Bounds inWindow,
                                   WindowGeometryStability.Bounds window,
                                   boolean active, boolean focused, long now) {
        return stability.observe(id, epoch, epoch, screen, inWindow, window, active, focused, now);
    }

    private static WindowGeometryStability.Bounds bounds(int left, int top, int right, int bottom) {
        return new WindowGeometryStability.Bounds(left, top, right, bottom);
    }
}
