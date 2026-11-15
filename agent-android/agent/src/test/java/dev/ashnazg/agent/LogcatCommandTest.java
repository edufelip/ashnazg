package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class LogcatCommandTest {
    @Test public void formatsEpochSinceTimestampWithOnlySafeLiteralCharacters() {
        String command = LogcatCommand.sinceEpochMillis(1800000000123L);
        assertEquals("logcat -v epoch -T 1800000000.123", command);
        assertTrue(command.matches("logcat -v epoch -T [0-9]+\\.[0-9]{3}"));
    }
}
