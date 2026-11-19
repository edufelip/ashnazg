package dev.ashnazg.fixture;

/** Exact captured-pixel contract: sixteen count bits followed by their complements. */
public final class FrameMarkerCode {
    public static final int CELLS = 32;

    private FrameMarkerCode() { }

    public static int cellColor(int count, int cell) {
        if (count < 0 || count > 65535 || cell < 0 || cell >= CELLS) {
            throw new IllegalArgumentException("count must be 0..65535 and cell 0..31");
        }
        int bit = (count >>> (cell % 16)) & 1;
        if (cell >= 16) bit ^= 1;
        return bit == 0 ? 0xff101010 : 0xfff0f0f0;
    }
}
