package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class LogSourceStateTest {
    @Test public void distinguishesUnavailableEndedAndAvailableSource() {
        LogSourceState state = new LogSourceState();
        assertNotNull(state.failure());
        state.started(); assertNull(state.failure());
        state.ended(); assertTrue(state.failure().contains("ended"));
    }

    @Test public void ignoresShutdownAsUnexpectedStreamFailure() {
        LogSourceState state = new LogSourceState();
        state.started(); state.close(); state.ended();
        assertNull(state.failure());
    }
}
