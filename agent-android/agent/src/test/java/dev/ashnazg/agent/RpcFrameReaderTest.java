package dev.ashnazg.agent;

import org.junit.Test;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.Assert.*;

public class RpcFrameReaderTest {
    @Test public void readsUtf8LineAndSignalsFirstByteOnlyOnce() throws Exception {
        AtomicInteger firstByte = new AtomicInteger();
        String result = RpcFrameReader.readLine(new ByteArrayInputStream("{\"text\":\"olá\"}\n".getBytes(StandardCharsets.UTF_8)), 64, firstByte::incrementAndGet);
        assertEquals("{\"text\":\"olá\"}", result);
        assertEquals(1, firstByte.get());
    }

    @Test public void rejectsOversizedAndUnterminatedFrames() throws Exception {
        try { RpcFrameReader.readLine(new ByteArrayInputStream("12345\n".getBytes(StandardCharsets.UTF_8)), 4, () -> {}); fail("oversized frame"); }
        catch (IOException expected) { assertTrue(expected.getMessage().contains("limit")); }
        try { RpcFrameReader.readLine(new ByteArrayInputStream("unfinished".getBytes(StandardCharsets.UTF_8)), 64, () -> {}); fail("unterminated frame"); }
        catch (IOException expected) { assertTrue(expected.getMessage().contains("incomplete")); }
    }
}
