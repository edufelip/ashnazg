package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class InstrumentationStartupTest {
    @Test public void explainsExclusiveUiAutomationFailure() {
        InstrumentationStartup.Failure failure = InstrumentationStartup.failure(
                new IllegalStateException("UiAutomationService is already registered!"));
        assertEquals(1, failure.exitCode);
        assertTrue(failure.message.contains("only one active UiAutomation service"));
    }

    @Test public void preservesOtherStartupErrorsWithoutMislabelingThem() {
        InstrumentationStartup.Failure failure = InstrumentationStartup.failure(new IllegalStateException("bind failed"));
        assertEquals("bind failed", failure.message);
        assertFalse(failure.message.contains("only one active UiAutomation service"));
    }
}
