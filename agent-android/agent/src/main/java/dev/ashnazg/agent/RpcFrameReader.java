package dev.ashnazg.agent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;

final class RpcFrameReader {
    private RpcFrameReader() {}

    static String readLine(InputStream input, int maxBytes, Runnable onFirstByte) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        boolean signaled = false;
        int value;
        while ((value = input.read()) != -1) {
            if (!signaled) { signaled = true; onFirstByte.run(); }
            if (value == '\n') {
                byte[] payload = bytes.toByteArray();
                int length = payload.length;
                if (length > 0 && payload[length - 1] == '\r') length--;
                try { return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                        .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(payload, 0, length)).toString(); }
                catch (CharacterCodingException e) { throw new IOException("invalid UTF-8", e); }
            }
            if (bytes.size() >= maxBytes) throw new IOException("message exceeds size limit");
            bytes.write(value);
        }
        if (bytes.size() == 0) return null;
        throw new IOException("incomplete line at end of stream");
    }
}
