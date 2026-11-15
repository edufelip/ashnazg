package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class SemanticIdentityTest {
    @Test public void detectsRecycledNodeReboundToDifferentTextOrTestId() {
        SemanticIdentity captured = new SemanticIdentity("dev.example:id/row", "Old row", "button", false);
        assertFalse(captured.matches(new SemanticIdentity("dev.example:id/row", "New row", "button", false)));
        assertFalse(captured.matches(new SemanticIdentity("dev.example:id/other", "Old row", "button", false)));
    }

    @Test public void allowsSameNodeToMoveAndSanitizesPasswordText() {
        SemanticIdentity input = new SemanticIdentity("input", "", "textbox", true);
        assertTrue(input.matches(new SemanticIdentity("input", "", "textbox", true)));
        assertTrue(input.matches(new SemanticIdentity("input", "••••", "textbox", true)));
        assertFalse(input.matches(new SemanticIdentity("other", "••••", "textbox", true)));
    }
}
