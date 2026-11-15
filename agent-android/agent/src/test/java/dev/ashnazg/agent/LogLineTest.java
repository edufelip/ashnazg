package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class LogLineTest {
    @Test public void parsesEpochTagWithLogcatPaddingAndColon() {
        String line = "1794749001.125  1234  1234 I AshnazgAgent: agent-started version=0.1.0";
        assertTrue(LogLine.matchesTag(line, "AshnazgAgent"));
        assertEquals(1794749001125L, LogLine.timestampMs(line));
    }

    @Test public void tagFilterDoesNotMatchSubstringsOrMessageText() {
        assertFalse(LogLine.matchesTag("1794749001.125  1234  1234 I AshnazgAgentWorker: message AshnazgAgent", "AshnazgAgent"));
        assertFalse(LogLine.matchesTag("1794749001.125  1234  1234 I OtherTag: AshnazgAgent", "AshnazgAgent"));
    }
}
