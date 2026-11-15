package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class LogSanitizerTest {
    @Test public void redactsExactGuestTokenWhilePreservingLogFields() {
        String source = "1794749001.125  1234 1234 I AshnazgAgent: token=secret-token ready";
        String result = LogSanitizer.redact(source, "secret-token");
        assertFalse(result.contains("secret-token"));
        assertTrue(result.contains("[REDACTED]"));
        assertTrue(result.contains("AshnazgAgent"));
    }
}
