package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class NodeTextTest {
    @Test public void omitsHintsAndPasswordsFromSnapshotText() {
        assertEquals("", NodeText.snapshotText("Input", false, true));
        assertEquals("", NodeText.snapshotText("secret", true, false));
        assertEquals("visible", NodeText.snapshotText("visible", false, false));
    }

    @Test public void appendTreatsHintAsEmptyAndRejectsUnclearSecureValue() {
        assertEquals("", NodeText.appendBase("Input", false, true));
        assertEquals("current", NodeText.appendBase("current", false, false));
        try { NodeText.appendBase("••••", true, false); fail("secure field needs clearing before append"); }
        catch (IllegalArgumentException expected) { assertTrue(expected.getMessage().contains("clear")); }
    }

    @Test public void recognizesOnlySafelyKnownNoOpTextMutations() {
        assertTrue(NodeText.isNoop("Input", false, "", false, true));
        assertTrue(NodeText.isNoop("", true, "", false, false));
        assertFalse(NodeText.isNoop("Framework你好", false, "", false, false));
        assertFalse(NodeText.isNoop("••••", false, "", true, false));
    }

    @Test public void confirmsExpectedValuesWithoutExposingSecureText() {
        assertTrue(NodeText.matchesExpected("Framework你好", "Framework你好", false, false));
        assertFalse(NodeText.matchesExpected("Framework你好", "", false, false));
        assertTrue(NodeText.matchesExpected("••••", "abcd", true, false));
        assertFalse(NodeText.matchesExpected("•••", "abcd", true, false));
        assertTrue(NodeText.matchesExpected("Password", "", true, true));
    }
}
