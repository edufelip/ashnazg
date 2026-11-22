package dev.ashnazg.fixture;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.view.View;

/** Draws app-owned count pixels independently of button ripple and text antialiasing. */
public final class FrameMarker extends View {
    private final Paint paint = new Paint();
    private int count;

    public FrameMarker(Context context) {
        super(context);
        paint.setAntiAlias(false);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_YES);
        setCount(0);
    }

    public void setCount(int value) {
        FrameMarkerCode.cellColor(value, 0); // Refuse wrapping or ambiguous encodings.
        count = value;
        setContentDescription("Frame count: " + value);
        invalidate();
    }

    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        for (int cell = 0; cell < FrameMarkerCode.CELLS; cell++) {
            paint.setColor(FrameMarkerCode.cellColor(count, cell));
            int left = cell * getWidth() / FrameMarkerCode.CELLS;
            int right = (cell + 1) * getWidth() / FrameMarkerCode.CELLS;
            canvas.drawRect(left, 0, right, getHeight(), paint);
        }
    }
}
