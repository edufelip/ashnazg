package dev.ashnazg.agent;

import org.junit.Test;
import java.util.List;
import static org.junit.Assert.*;

public class NavigationPostconditionTest {
    private static final NavigationPostcondition.Bounds FRAME = new NavigationPostcondition.Bounds(0, 0, 1080, 2400);

    @Test public void openingWindowAdditionAndOldClosingRootDoNotSatisfyBack() {
        NavigationPostcondition.State baseline = state(20, "fixture", "Deep link", List.of(app(10), app(20)), 4, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State addedOnly = state(20, "fixture", "Deep link", List.of(app(10), app(20), app(30)), 5, true, true);
        assertFalse(policy.observe(addedOnly, 0, 5000));
        assertFalse(policy.observe(addedOnly, 1000, 5000));
        NavigationPostcondition.State incoherent = state(20, "fixture", "Deep link", List.of(app(10), app(20)), 5, true, true, false);
        assertFalse(policy.observe(incoherent, 1200, 5000));
    }

    @Test public void differentActivityWindowInSamePackageNeeds500msStableState() {
        NavigationPostcondition.State baseline = state(20, "fixture", "Deep link Count: 0", List.of(app(10), app(20)), 4, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State returned = state(10, "fixture", "Fixture Count: 2", List.of(app(10)), 6, true, true);
        assertFalse(policy.observe(returned, 0, 5000));
        assertFalse(policy.observe(returned, 499, 5000));
        assertTrue(policy.observe(returned, 500, 5000));
    }

    @Test public void semanticRouteChangeWithinSameWindowCanSettle() {
        NavigationPostcondition.State baseline = state(7, "fixture", "Home", List.of(app(7)), 1, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State route = state(7, "fixture", "Settings", List.of(app(7)), 2, true, true);
        assertFalse(policy.observe(route, 10, 5000));
        assertTrue(policy.observe(route, 510, 5000));
    }

    @Test public void removingImeIsARecognizableBackOutcome() {
        NavigationPostcondition.State baseline = state(7, "fixture", "Input focused", List.of(app(7), ime(9)), 1, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State imeDismissed = state(7, "fixture", "Input focused", List.of(app(7)), 2, true, true);
        assertFalse(policy.observe(imeDismissed, 10, 5000));
        assertTrue(policy.observe(imeDismissed, 510, 5000));
    }

    @Test public void geometryGenerationOrFocusChangesResetTheStabilityInterval() {
        NavigationPostcondition.State baseline = state(20, "fixture", "Deep link", List.of(app(20)), 4, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State returned = state(10, "fixture", "Count: 2", List.of(app(10)), 5, true, true);
        assertFalse(policy.observe(returned, 0, 5000));
        NavigationPostcondition.State moved = state(10, "fixture", "Count: 2", List.of(app(10, new NavigationPostcondition.Bounds(0, 2, 1080, 2402))), 5, true, true);
        assertFalse(policy.observe(moved, 200, 5000));
        assertFalse(policy.observe(moved, 699, 5000));
        assertFalse(policy.observe(state(10, "fixture", "Count: 2", List.of(app(10)), 6, true, true), 700, 5000));
        assertFalse(policy.observe(state(10, "fixture", "Count: 2", List.of(app(10)), 6, false, false), 800, 5000));
        NavigationPostcondition.State focused = state(10, "fixture", "Count: 2", List.of(app(10)), 6, true, false);
        assertFalse(policy.observe(focused, 900, 5000));
        assertTrue(policy.observe(focused, 1400, 5000));
    }

    @Test public void rootNeedsAMatchingWindowWithNonemptyBounds() {
        NavigationPostcondition.State baseline = state(20, "fixture", "Deep link", List.of(app(20)), 4, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State missing = state(10, "fixture", "Count: 2", List.of(app(11)), 5, true, true);
        assertFalse(policy.observe(missing, 0, 5000));
        assertFalse(policy.observe(missing, 500, 5000));
        NavigationPostcondition.State empty = state(10, "fixture", "Count: 2",
                List.of(app(10, new NavigationPostcondition.Bounds(0, 0, 0, 0))), 5, true, true);
        assertFalse(policy.observe(empty, 600, 5000));
        assertFalse(policy.observe(empty, 1100, 5000));
    }

    @Test public void sharedFiveSecondDeadlinePreventsLateCompletion() {
        NavigationPostcondition.State baseline = state(20, "fixture", "Deep link", List.of(app(20)), 4, true, true);
        NavigationPostcondition policy = new NavigationPostcondition(baseline);
        NavigationPostcondition.State returned = state(10, "fixture", "Count: 2", List.of(app(10)), 5, true, true);
        assertFalse(policy.observe(returned, 4500, 5000));
        assertFalse(policy.observe(returned, 5000, 5000));
        assertFalse(policy.observe(returned, 5500, 5000));
    }

    private static NavigationPostcondition.State state(int rootId, String pkg, String semantic,
                                                        List<NavigationPostcondition.Window> windows,
                                                        long generation, boolean active, boolean focused) {
        return state(rootId, pkg, semantic, windows, generation, active, focused, true);
    }
    private static NavigationPostcondition.State state(int rootId, String pkg, String semantic,
                                                        List<NavigationPostcondition.Window> windows,
                                                        long generation, boolean active, boolean focused, boolean coherent) {
        return new NavigationPostcondition.State(rootId, pkg, semantic, windows, active, focused, generation, coherent);
    }
    private static NavigationPostcondition.Window app(int id) { return app(id, FRAME); }
    private static NavigationPostcondition.Window app(int id, NavigationPostcondition.Bounds frame) {
        return new NavigationPostcondition.Window(id, NavigationPostcondition.APPLICATION, true, true, frame);
    }
    private static NavigationPostcondition.Window ime(int id) {
        return new NavigationPostcondition.Window(id, NavigationPostcondition.INPUT_METHOD, true, false,
                new NavigationPostcondition.Bounds(0, 1700, 1080, 2400));
    }
}
