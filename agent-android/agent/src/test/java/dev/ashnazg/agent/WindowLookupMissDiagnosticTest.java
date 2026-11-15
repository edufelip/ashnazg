package dev.ashnazg.agent;

import org.junit.Test;
import static org.junit.Assert.*;

public class WindowLookupMissDiagnosticTest {
    @Test public void diagnosticIsBoundedAndContainsOnlyWindowIds() {
        int[] firstIds = {1, 2, 3, 4, 5, 6, 7, 8};

        String diagnostic = WindowLookupMissDiagnostic.format(99, 11, firstIds);

        assertEquals("window-lookup-miss target=99 visibleIds=[1,2,3,4,5,6,7,8] omitted=3", diagnostic);
        assertFalse(diagnostic.contains("token"));
        assertTrue(diagnostic.length() < 128);
    }

    @Test public void diagnosticHandlesEmptyWindowList() {
        assertEquals("window-lookup-miss target=99 visibleIds=[] omitted=0",
                WindowLookupMissDiagnostic.format(99, 0, new int[0]));
    }
}
