package dev.ashnazg.agent;

import android.app.Instrumentation;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityService;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.Typeface;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityEvent;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;

final class UiController {
    private static final String TAG = "AshnazgAgent";
    private static final int MAX_NODES = 10000, MAX_DEPTH = 128, MAX_SCROLLS = 12;
    private final UiAutomation automation;
    private final Instrumentation instrumentation;
    private final String secretToken;
    private final Deque<String> logRing = new ArrayDeque<>();
    private final Object logLock = new Object();
    private final Object accessibilityEventMonitor = new Object();
    private final AtomicLong lastAccessibilityEventAtMs = new AtomicLong();
    private ParcelFileDescriptor logPipe;
    private final LogSourceState logSource = new LogSourceState();
    private Thread logThread;
    private boolean logTruncated;
    private int nextSnapshotId = 1;
    private Snapshot current;

    UiController(UiAutomation automation, Instrumentation instrumentation, String secretToken) {
        this.automation = automation;
        this.instrumentation = instrumentation;
        this.secretToken = secretToken;
        automation.setOnAccessibilityEventListener(event -> {
            lastAccessibilityEventAtMs.set(SystemClock.elapsedRealtime());
            synchronized (accessibilityEventMonitor) { accessibilityEventMonitor.notifyAll(); }
            event.recycle();
        });
        startLogReader();
        Log.i(TAG, "agent-started version=0.1.0");
    }

    synchronized JSONObject snapshot(boolean includeInvisible) throws JSONException {
        return capture(includeInvisible).json;
    }

    synchronized JSONObject act(JSONObject params) throws Exception {
        String action = stringParam(params, "action", "");
        if (!List.of("tap", "long_press", "type", "clear", "scroll_to", "back", "deep_link").contains(action))
            throw new IllegalArgumentException("unsupported action");
        boolean requiresTarget = List.of("tap", "long_press", "type", "clear", "scroll_to").contains(action);
        if (params.has("target") && !(params.opt("target") instanceof JSONObject)) throw new IllegalArgumentException("target must be an object");
        JSONObject targetJson = params.optJSONObject("target");
        SelectorResolver.Target target = requiresTarget ? parseTarget(targetJson) : null;
        Snapshot before;
        if (target != null && target.id != null) {
            if (current == null) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "stale target: no prior snapshot exists");
            before = current;
        } else if (target != null) before = capture(false);
        else before = current;

        boolean acted = false;
        boolean navigationObserved = true;
        boolean mutationObserved = true;
        switch (action) {
            case "tap": case "long_press": case "type": case "clear": {
                Record record = resolve(before, target);
                if ("tap".equals(action)) tap(record, false);
                else if ("long_press".equals(action)) tap(record, true);
                else mutationObserved = edit(record, "type".equals(action), stringParam(params, "text", null));
                acted = true;
                break;
            }
            case "scroll_to": {
                String direction = stringParam(params, "direction", "down");
                if (!List.of("up", "down", "left", "right").contains(direction)) throw new IllegalArgumentException("invalid scroll direction");
                for (int i = 0; i <= MAX_SCROLLS; i++) {
                    before = target.id == null ? capture(false) : before;
                    try { resolve(before, target); acted = true; break; }
                    catch (SelectorResolver.SelectorException e) {
                        if (e.failure != SelectorResolver.Failure.NOT_FOUND || i == MAX_SCROLLS) throw e;
                        swipe(direction);
                        settle();
                    }
                }
                break;
            }
            case "back":
                boolean[] backAccepted = new boolean[1];
                navigationObserved = awaitNavigation(() -> backAccepted[0] = automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK));
                if (!backAccepted[0]) throw new IllegalStateException("back action was not accepted");
                acted = true;
                break;
            case "deep_link": {
                String link = stringParam(params, "text", "");
                Uri uri = Uri.parse(link);
                if (uri.getScheme() == null) throw new IllegalArgumentException("deep_link text must be an absolute URI");
                Intent intent = new Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                navigationObserved = awaitNavigation(() -> instrumentation.getTargetContext().startActivity(intent));
                acted = true;
                break;
            }
        }
        long actionCompletedAtMs = SystemClock.elapsedRealtime();
        boolean quiet = !acted || awaitQuietWindow(actionCompletedAtMs);
        boolean settled = !acted || (NavigationBarrier.settled(navigationObserved, true) &&
                NavigationBarrier.afterMutation(mutationObserved, true) && quiet);
        automation.clearCache();
        JSONObject fresh = capture(false).json;
        return new JSONObject().put("snapshot", fresh).put("settled", settled);
    }

    synchronized JSONObject screenshot(JSONObject params) throws Exception {
        String format = stringParam(params, "format", "webp").toLowerCase(Locale.ROOT);
        if (!format.equals("webp") && !format.equals("png")) throw new IllegalArgumentException("format must be webp or png");
        boolean overlay = booleanParam(params, "somOverlay", false);
        Snapshot snap = capture(false);
        Bitmap bitmap = automation.takeScreenshot();
        if (bitmap == null) throw new IllegalStateException("UiAutomation.takeScreenshot returned no image");
        int width = bitmap.getWidth(), height = bitmap.getHeight();
        if (overlay) {
            Bitmap mutable = bitmap.copy(Bitmap.Config.ARGB_8888, true);
            bitmap.recycle();
            if (mutable == null) throw new IllegalStateException("could not create mutable screenshot for overlay");
            bitmap = mutable;
        }
        JSONArray marks = new JSONArray();
        if (overlay) drawOverlay(bitmap, snap, marks);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        Bitmap.CompressFormat compression = format.equals("png") ? Bitmap.CompressFormat.PNG : Bitmap.CompressFormat.WEBP_LOSSY;
        if (!bitmap.compress(compression, format.equals("png") ? 100 : 82, out)) throw new IllegalStateException("screenshot encoding failed");
        bitmap.recycle();
        String mime = format.equals("png") ? "image/png" : "image/webp";
        return new JSONObject().put("mimeType", mime).put("data", Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP))
                .put("snapshotId", snap.id).put("marks", marks).put("width", width).put("height", height);
    }

    JSONObject logs(JSONObject params) throws JSONException {
        String failure = logSource.failure();
        if (failure != null) throw new IllegalStateException("app_logs unavailable: " + failure);
        String tagFilter = stringParam(params, "tagFilter", null);
        long since = longParam(params, "sinceMs", 0);
        if (since < 0) throw new IllegalArgumentException("sinceMs must be nonnegative");
        JSONArray lines = new JSONArray();
        synchronized (logLock) {
            for (String line : logRing) {
                if (tagFilter != null && !LogLine.matchesTag(line, tagFilter)) continue;
                if (since > 0 && LogLine.timestampMs(line) < since) continue;
                lines.put(line);
            }
            return new JSONObject().put("lines", lines).put("truncated", logTruncated);
        }
    }

    private Snapshot capture(boolean includeInvisible) throws JSONException {
        releaseCurrent();
        int id = nextSnapshotId++;
        Snapshot snap = new Snapshot(id);
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null) throw new IllegalStateException("no active accessibility window");
        walk(root, 0, snap, includeInvisible);
        root.recycle();
        snap.json = toJson(snap);
        current = snap;
        return snap;
    }

    private void walk(AccessibilityNodeInfo node, int depth, Snapshot snap, boolean includeInvisible) throws JSONException {
        if (depth > MAX_DEPTH || snap.visited >= MAX_NODES) { snap.truncated = true; return; }
        snap.visited++;
        boolean visible = node.isVisibleToUser();
        CharSequence raw = node.getText();
        boolean password = node.isPassword();
        String text = NodeText.snapshotText(raw, password, node.isShowingHintText());
        String viewId = node.getViewIdResourceName();
        if ((viewId == null || viewId.isEmpty()) && node.getExtras() != null) {
            Bundle extras = node.getExtras();
            CharSequence tag = extras.getCharSequence("androidx.compose.ui.semantics.testTag");
            if (tag != null) viewId = tag.toString();
        }
        CharSequence description = node.getContentDescription();
        String contentDescription = description == null || password ? "" : description.toString();
        CharSequence rawHint = node.getHintText();
        String hint = rawHint == null || password ? "" : rawHint.toString();
        boolean enabled = node.isEnabled(), clickable = node.isClickable();
        boolean editable = node.isEditable();
        boolean meaningful = !text.isEmpty() || !contentDescription.isEmpty() || viewId != null || clickable || node.isLongClickable() || editable || node.isCheckable() || node.isScrollable();
        if (text.isEmpty() && !password && !node.isShowingHintText() && !contentDescription.isEmpty()) text = contentDescription;
        if (meaningful && (includeInvisible || visible)) {
            int nodeId = snap.records.size() + 1;
            AccessibilityNodeInfo retained = AccessibilityNodeInfo.obtain(node);
            Rect rect = new Rect(); node.getBoundsInScreen(rect);
            Record record = new Record(nodeId, viewId, text, role(node, editable), rect, enabled, node.isFocused(), clickable,
                    editable, password, node.isCheckable(), node.isChecked(), node.isSelected(), node.isScrollable(), node.isLongClickable(),
                    hint, contentDescription, retained);
            snap.records.add(record);
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) { walk(child, depth + 1, snap, includeInvisible); child.recycle(); }
            if (snap.visited >= MAX_NODES) { snap.truncated = true; break; }
        }
    }

    private JSONObject toJson(Snapshot snap) throws JSONException {
        JSONArray nodes = new JSONArray();
        for (Record record : snap.records) {
            JSONObject item = new JSONObject().put("id", record.id).put("role", record.role).put("text", record.text)
                    .put("rect", new JSONArray().put(record.rect.left).put(record.rect.top).put(record.rect.right).put(record.rect.bottom))
                    .put("enabled", record.enabled).put("focused", record.focused).put("clickable", record.clickable)
                    .put("checkable", record.checkable).put("checked", record.checked).put("selected", record.selected)
                    .put("scrollable", record.scrollable).put("password", record.password).put("longClickable", record.longClickable);
            if (record.testId != null && !record.testId.isEmpty()) item.put("testId", record.testId);
            if (!record.hint.isEmpty()) item.put("hint", record.hint);
            if (!record.contentDescription.isEmpty()) item.put("contentDescription", record.contentDescription);
            nodes.put(item);
        }
        return new JSONObject().put("snapshotId", snap.id).put("nodes", nodes).put("truncated", snap.truncated);
    }

    private Record resolve(Snapshot snap, SelectorResolver.Target target) {
        if (snap == null) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "take a snapshot before using an ID selector");
        List<SelectorResolver.Node> nodes = new ArrayList<>();
        Map<Integer, Record> byId = new HashMap<>();
        for (Record record : snap.records) { nodes.add(new SelectorResolver.Node(record.id, record.testId, record.text, record.enabled)); byId.put(record.id, record); }
        SelectorResolver.Node selected = SelectorResolver.resolve(nodes, target, snap.id);
        Record record = byId.get(selected.id);
        refreshRecord(record);
        return record;
    }

    private Rect refreshRecord(Record record) {
        if (!record.node.refresh() || !record.node.isVisibleToUser())
            throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "stale target is no longer visible");
        if (!record.node.isEnabled()) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.DISABLED, "target node is disabled");
        boolean freshPassword = record.node.isPassword();
        boolean showingHint = record.node.isShowingHintText();
        String freshText = NodeText.snapshotText(record.node.getText(), freshPassword, showingHint);
        String freshId = record.node.getViewIdResourceName();
        if (freshId == null || freshId.isEmpty()) {
            Bundle extras = record.node.getExtras();
            CharSequence tag = extras == null ? null : extras.getCharSequence("androidx.compose.ui.semantics.testTag");
            if (tag != null) freshId = tag.toString();
        }
        CharSequence currentDescription = record.node.getContentDescription();
        if (freshText.isEmpty() && !freshPassword && !showingHint && currentDescription != null) freshText = currentDescription.toString();
        SemanticIdentity captured = new SemanticIdentity(record.testId, record.text, record.role, record.password);
        SemanticIdentity fresh = new SemanticIdentity(freshId, freshText, role(record.node, record.node.isEditable()), freshPassword);
        if (!captured.matches(fresh)) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "stale target identity changed since the snapshot");
        record.node.getBoundsInScreen(record.rect);
        if (record.rect.isEmpty()) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "target has no actionable bounds");
        return record.rect;
    }

    private SelectorResolver.Target parseTarget(JSONObject json) {
        if (json == null) throw new IllegalArgumentException("target is required");
        String testId = stringParam(json, "testId", null);
        String text = stringParam(json, "text", null);
        Integer id = json.has("id") ? integerParam(json, "id") : null;
        Integer snapshotId = json.has("snapshotId") ? integerParam(json, "snapshotId") : null;
        return new SelectorResolver.Target(testId, text, id, snapshotId);
    }

    private void tap(Record record, boolean longPress) {
        stabilizeBounds(record);
        float x = record.rect.centerX(), y = record.rect.centerY();
        injectTap(x, y, longPress ? 800 : 80);
    }

    private boolean edit(Record record, boolean append, String value) throws Exception {
        if (!record.editable) throw new IllegalArgumentException("target is not editable");
        if (append && value == null) throw new IllegalArgumentException("type requires text");
        if (NodeText.isNoop(record.node.getText(), append, value, record.password, record.node.isShowingHintText())) return true;
        String newText = value == null ? "" : value;
        if (append) {
            String existing = NodeText.appendBase(record.node.getText(), record.password, record.node.isShowingHintText());
            newText = existing + newText;
        }
        Bundle args = new Bundle(); args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, newText);
        if (!record.node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args))
            throw new IllegalStateException("node rejected ACTION_SET_TEXT");
        return awaitExpectedText(record, newText);
    }

    private void swipe(String direction) {
        Rect bounds = new Rect();
        if (current != null) for (Record r : current.records) if (r.node.isScrollable()) { bounds = stabilizeBounds(r); break; }
        if (bounds.isEmpty()) {
            android.util.DisplayMetrics metrics = instrumentation.getTargetContext().getResources().getDisplayMetrics();
            bounds = new Rect(0, 0, metrics.widthPixels, metrics.heightPixels);
        }
        float cx = bounds.centerX(), cy = bounds.centerY(), fromX = cx, fromY = cy, toX = cx, toY = cy;
        switch (direction) {
            case "down": fromY = bounds.top + bounds.height() * .8f; toY = bounds.top + bounds.height() * .2f; break;
            case "up": fromY = bounds.top + bounds.height() * .2f; toY = bounds.top + bounds.height() * .8f; break;
            case "left": fromX = bounds.left + bounds.width() * .8f; toX = bounds.left + bounds.width() * .2f; break;
            case "right": fromX = bounds.left + bounds.width() * .2f; toX = bounds.left + bounds.width() * .8f; break;
        }
        long downTime = SystemClock.uptimeMillis();
        injectMotion(downTime, downTime, MotionEvent.ACTION_DOWN, fromX, fromY);
        for (int i = 1; i <= 12; i++) {
            float fraction = i / 12f;
            long eventTime = downTime + i * 25L;
            injectMotion(downTime, eventTime, MotionEvent.ACTION_MOVE, fromX + (toX - fromX) * fraction, fromY + (toY - fromY) * fraction);
            SystemClock.sleep(25);
        }
        injectMotion(downTime, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, toX, toY);
    }

    private void injectTap(float x, float y, long durationMs) {
        long downTime = SystemClock.uptimeMillis();
        injectMotion(downTime, downTime, MotionEvent.ACTION_DOWN, x, y);
        SystemClock.sleep(durationMs);
        injectMotion(downTime, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y);
    }
    private void injectMotion(long downTime, long eventTime, int action, float x, float y) {
        MotionEvent event = MotionEvent.obtain(downTime, eventTime, action, x, y, 0);
        event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        boolean accepted = automation.injectInputEvent(event, true);
        event.recycle();
        if (!accepted) throw new IllegalStateException("input event was not accepted");
    }

    private boolean settle() {
        return awaitQuietWindow(SystemClock.elapsedRealtime());
    }

    private Rect stabilizeBounds(Record record) {
        long deadline = SystemClock.elapsedRealtime() + 2000;
        TargetBoundsStability stability = new TargetBoundsStability();
        while (SystemClock.elapsedRealtime() <= deadline) {
            automation.clearCache();
            Rect bounds = refreshRecord(record);
            long now = SystemClock.elapsedRealtime();
            if (stability.observe(bounds.left, bounds.top, bounds.right, bounds.bottom, now)) return record.rect;
            SystemClock.sleep(16);
        }
        throw new IllegalStateException("target bounds did not stabilize within 2 seconds");
    }

    private boolean awaitQuietWindow(long actionCompletedAtMs) {
        long deadline = actionCompletedAtMs + 2000;
        while (true) {
            long now = SystemClock.elapsedRealtime();
            long lastEvent = lastAccessibilityEventAtMs.get();
            long remainingQuiet = QuietWindowPolicy.remainingMs(actionCompletedAtMs, lastEvent, now, 120);
            if (remainingQuiet == 0) return true;
            long remainingOverall = deadline - now;
            if (remainingOverall <= 0) return false;
            synchronized (accessibilityEventMonitor) {
                if (lastAccessibilityEventAtMs.get() != lastEvent) continue;
                try { accessibilityEventMonitor.wait(Math.min(remainingQuiet, remainingOverall)); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return false; }
            }
        }
    }

    private boolean awaitNavigation(Runnable action) {
        try {
            AccessibilityEvent result = automation.executeAndWaitForEvent(action, event ->
                    event.getEventType() == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
                            event.getEventType() == AccessibilityEvent.TYPE_WINDOWS_CHANGED, 5000);
            result.recycle();
            return true;
        } catch (TimeoutException e) { return false; }
    }

    private boolean awaitExpectedText(Record record, String expected) {
        long deadline = SystemClock.elapsedRealtime() + 2000;
        while (true) {
            automation.clearCache();
            boolean refreshed = record.node.refresh();
            if (refreshed && record.node.isVisibleToUser()) {
                CharSequence actual = record.node.getText();
                if (NodeText.matchesExpected(actual, expected, record.node.isPassword(), record.node.isShowingHintText()))
                    return true;
            } else if (!refreshed) {
                return false;
            }
            long remaining = deadline - SystemClock.elapsedRealtime();
            if (remaining <= 0) return false;
            long lastEvent = lastAccessibilityEventAtMs.get();
            synchronized (accessibilityEventMonitor) {
                if (lastAccessibilityEventAtMs.get() != lastEvent) continue;
                try { accessibilityEventMonitor.wait(Math.min(25, remaining)); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return false; }
            }
        }
    }

    private void drawOverlay(Bitmap bitmap, Snapshot snap, JSONArray marks) throws JSONException {
        Canvas canvas = new Canvas(bitmap);
        Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG); paint.setColor(Color.MAGENTA); paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(3);
        Paint label = new Paint(Paint.ANTI_ALIAS_FLAG); label.setColor(Color.WHITE); label.setTextSize(24); label.setTypeface(Typeface.DEFAULT_BOLD);
        Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG); fill.setColor(0xCCAA00AA); fill.setStyle(Paint.Style.FILL);
        for (Record record : snap.records) {
            if (!record.clickable && !record.longClickable && !record.editable && !record.checkable) continue;
            canvas.drawRect(record.rect, paint);
            float x = Math.max(0, record.rect.left), y = Math.max(24, record.rect.top);
            canvas.drawRect(x, y - 24, x + 42, y + 4, fill); canvas.drawText(Integer.toString(record.id), x + 8, y - 4, label);
            marks.put(new JSONObject().put("id", record.id).put("rect", new JSONArray().put(record.rect.left).put(record.rect.top).put(record.rect.right).put(record.rect.bottom)));
        }
    }

    private void startLogReader() {
        try {
            long startWindowMs = System.currentTimeMillis();
            logPipe = automation.executeShellCommand(LogcatCommand.sinceEpochMillis(startWindowMs));
            if (logPipe == null) throw new IllegalStateException("logcat command returned no pipe");
            logSource.started();
            logThread = new Thread(() -> {
                try (BufferedReader reader = new BufferedReader(new InputStreamReader(new ParcelFileDescriptor.AutoCloseInputStream(logPipe)))) {
                    String line;
                    while ((line = reader.readLine()) != null) addLog(line);
                    logSource.ended();
                } catch (Exception e) { logSource.failed(e); }
            }, "ashnazg-logcat-reader");
            logThread.setDaemon(true); logThread.start();
        } catch (Exception e) { logSource.failed(e); }
    }
    private void addLog(String line) {
        String sanitized = LogSanitizer.redact(line, secretToken);
        synchronized (logLock) { if (logRing.size() == 2000) { logRing.removeFirst(); logTruncated = true; } logRing.addLast(sanitized); }
    }

    private String role(AccessibilityNodeInfo node, boolean editable) {
        if (editable) return "textbox";
        CharSequence name = node.getClassName();
        String cls = name == null ? "" : name.toString();
        if (node.isHeading()) return "heading";
        if (cls.endsWith("Button") || cls.endsWith("ImageButton")) return "button";
        if (cls.endsWith("CheckBox")) return "checkbox";
        if (cls.endsWith("RadioButton")) return "radio";
        if (cls.endsWith("Switch")) return "switch";
        if (cls.endsWith("TextView")) return "text";
        if (node.isScrollable()) return "scrollable";
        return "view";
    }

    synchronized void close() {
        logSource.close();
        try { if (logPipe != null) logPipe.close(); } catch (Exception ignored) { }
        if (logThread != null) logThread.interrupt();
        releaseCurrent();
    }

    private String stringParam(JSONObject object, String key, String fallback) {
        if (!object.has(key)) return fallback;
        Object value = object.opt(key);
        if (!(value instanceof String)) throw new IllegalArgumentException(key + " must be a string");
        return (String) value;
    }
    boolean booleanParam(JSONObject object, String key, boolean fallback) {
        if (!object.has(key)) return fallback;
        Object value = object.opt(key);
        if (!(value instanceof Boolean)) throw new IllegalArgumentException(key + " must be a boolean");
        return (Boolean) value;
    }
    private Integer integerParam(JSONObject object, String key) {
        long value = longParam(object, key, Long.MIN_VALUE);
        if (value < Integer.MIN_VALUE || value > Integer.MAX_VALUE) throw new IllegalArgumentException(key + " must fit a 32-bit integer");
        return (int) value;
    }
    private long longParam(JSONObject object, String key, long fallback) {
        if (!object.has(key)) return fallback;
        Object value = object.opt(key);
        if (!(value instanceof Number)) throw new IllegalArgumentException(key + " must be an integer");
        double number = ((Number) value).doubleValue();
        if (!Double.isFinite(number) || number != Math.rint(number) || number < Long.MIN_VALUE || number > Long.MAX_VALUE)
            throw new IllegalArgumentException(key + " must be an integer");
        return ((Number) value).longValue();
    }

    private void releaseCurrent() { if (current != null) { for (Record record : current.records) record.node.recycle(); current = null; } }
    private static final class Snapshot {
        final int id; final List<Record> records = new ArrayList<>(); int visited; boolean truncated; JSONObject json;
        Snapshot(int id) { this.id = id; }
    }
    private static final class Record {
        final int id; final String testId, text, role, hint, contentDescription; final Rect rect; final boolean enabled, focused, clickable, editable, password, checkable, checked, selected, scrollable, longClickable; final AccessibilityNodeInfo node;
        Record(int id, String testId, String text, String role, Rect rect, boolean enabled, boolean focused, boolean clickable, boolean editable, boolean password, boolean checkable, boolean checked, boolean selected, boolean scrollable, boolean longClickable, String hint, String contentDescription, AccessibilityNodeInfo node) {
            this.id=id; this.testId=testId; this.text=text; this.role=role; this.rect=rect; this.enabled=enabled; this.focused=focused; this.clickable=clickable; this.editable=editable; this.password=password; this.checkable=checkable; this.checked=checked; this.selected=selected; this.scrollable=scrollable; this.longClickable=longClickable; this.hint=hint; this.contentDescription=contentDescription; this.node=node;
        }
    }
}
