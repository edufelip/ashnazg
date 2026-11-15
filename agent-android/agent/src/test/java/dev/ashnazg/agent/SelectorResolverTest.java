package dev.ashnazg.agent;

import org.junit.Test;
import java.util.List;
import static org.junit.Assert.*;

public class SelectorResolverTest {
    private static SelectorResolver.Node node(int id, String testId, String text, boolean enabled) {
        return new SelectorResolver.Node(id, testId, text, enabled);
    }

    @Test public void resolvesOnlyUniqueTestId() {
        List<SelectorResolver.Node> nodes = List.of(node(3, "submit", "Pay", true));
        assertEquals(3, SelectorResolver.resolve(nodes, new SelectorResolver.Target("submit", null, null, null), 9).id);
    }

    @Test public void qualifiedResourceIdStaysPackageSpecific() {
        List<SelectorResolver.Node> nodes = List.of(
                node(1, "dev.first:id/password", "First", true),
                node(2, "dev.second:id/password", "Second", true));
        assertEquals(2, SelectorResolver.resolve(nodes, new SelectorResolver.Target("dev.second:id/password", null, null, null), 9).id);
    }

    @Test public void unqualifiedResourceIdAliasRejectsCrossPackageAmbiguity() {
        List<SelectorResolver.Node> nodes = List.of(
                node(1, "dev.first:id/password", "First", true),
                node(2, "dev.second:id/password", "Second", true));
        assertEquals(SelectorResolver.Failure.AMBIGUOUS,
                failure(() -> SelectorResolver.resolve(nodes, new SelectorResolver.Target("password", null, null, null), 9)));
    }

    @Test public void rejectsAmbiguousAndMissingSelectors() {
        List<SelectorResolver.Node> nodes = List.of(node(1, null, "Continue", true), node(2, null, "Continue", true));
        assertEquals(SelectorResolver.Failure.AMBIGUOUS,
                failure(() -> SelectorResolver.resolve(nodes, new SelectorResolver.Target(null, "Continue", null, null), 9)));
        assertEquals(SelectorResolver.Failure.NOT_FOUND,
                failure(() -> SelectorResolver.resolve(nodes, new SelectorResolver.Target("absent", null, null, null), 9)));
    }

    @Test public void rejectsStaleNumericIdsAndDisabledNodes() {
        assertEquals(SelectorResolver.Failure.STALE,
                failure(() -> SelectorResolver.resolve(List.of(node(1, null, "Go", true)), new SelectorResolver.Target(null, null, 1, 8), 9)));
        assertEquals(SelectorResolver.Failure.DISABLED,
                failure(() -> SelectorResolver.resolve(List.of(node(1, "go", "Go", false)), new SelectorResolver.Target("go", null, null, null), 9)));
    }

    @Test public void requiresExactlyOneSelectorAndMatchingSnapshotForId() {
        assertEquals(SelectorResolver.Failure.INVALID_SELECTOR,
                failure(() -> SelectorResolver.resolve(List.of(), new SelectorResolver.Target(null, null, null, null), 9)));
        assertEquals(SelectorResolver.Failure.INVALID_SELECTOR,
                failure(() -> SelectorResolver.resolve(List.of(), new SelectorResolver.Target("a", "b", null, null), 9)));
        assertEquals(SelectorResolver.Failure.INVALID_SELECTOR,
                failure(() -> SelectorResolver.resolve(List.of(), new SelectorResolver.Target(null, null, 1, null), 9)));
    }

    private interface Operation { void run(); }
    private static SelectorResolver.Failure failure(Operation op) {
        try { op.run(); fail("expected selector failure"); return null; }
        catch (SelectorResolver.SelectorException e) { return e.failure; }
    }
}
