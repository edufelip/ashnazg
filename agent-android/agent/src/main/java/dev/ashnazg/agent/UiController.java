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
import java.util.Collections;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
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
    private final AtomicLong windowEventGeneration = new AtomicLong();
    private ParcelFileDescriptor logPipe;
    private final LogSourceState logSource = new LogSourceState();
    private Thread logThread;
    private boolean logTruncated;
    private int nextSnapshotId = 1;
    private int lastReadyWindowId = -1;
    private long lastReadyWindowGeneration = -1;
    private WindowGeometryStability.Bounds lastReadyWindowBounds;
    private Snapshot current;

    UiController(UiAutomation automation, Instrumentation instrumentation, String secretToken) {
        this.automation = automation;
        this.instrumentation = instrumentation;
        this.secretToken = secretToken;
        automation.setOnAccessibilityEventListener(event -> {
            lastAccessibilityEventAtMs.set(SystemClock.elapsedRealtime());
            int type = event.getEventType();
            if (type == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED || type == AccessibilityEvent.TYPE_WINDOWS_CHANGED)
                windowEventGeneration.incrementAndGet();
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
        } else if (target != null) before = ("tap".equals(action) || "long_press".equals(action)) ? null : capture(false);
        else before = current;

        boolean acted = false;
        boolean navigationObserved = true;
        boolean mutationObserved = true;
        long[] navigationDeadline = {0};
        switch (action) {
            case "tap": case "long_press": case "type": case "clear": {
                if ("tap".equals(action) || "long_press".equals(action)) {
                    PointerCoordinates pointer = preparePointerInput(before, target);
                    injectTap(pointer.x, pointer.y, "long_press".equals(action) ? 800 : 80);
                } else {
                    Record record = resolve(before, target);
                    mutationObserved = edit(record, "type".equals(action), stringParam(params, "text", null));
                }
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
                navigationObserved = awaitNavigation(() -> backAccepted[0] = automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK), navigationDeadline);
                if (!backAccepted[0]) throw new IllegalStateException("back action was not accepted");
                acted = true;
                break;
            case "deep_link": {
                String link = stringParam(params, "text", "");
                Uri uri = Uri.parse(link);
                if (uri.getScheme() == null) throw new IllegalArgumentException("deep_link text must be an absolute URI");
                Intent intent = new Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                navigationObserved = awaitNavigation(() -> instrumentation.getTargetContext().startActivity(intent), navigationDeadline);
                acted = true;
                break;
            }
        }
        long actionCompletedAtMs = SystemClock.elapsedRealtime();
        long quietDeadline = navigationDeadline[0] == 0 ? actionCompletedAtMs + 2000 : navigationDeadline[0];
        boolean quiet = !acted || awaitQuietWindow(actionCompletedAtMs, quietDeadline);
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
        if (root == null) throw new NoActiveWindowException();
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
            CharSequence rawPackage = node.getPackageName();
            String packageName = rawPackage == null ? null : rawPackage.toString();
            Record record = new Record(nodeId, viewId, text, role(node, editable), packageName, rect, enabled, node.isFocused(), clickable,
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
        Record record = selectRecord(snap, target);
        refreshRecord(record);
        return record;
    }

    private Record selectRecord(Snapshot snap, SelectorResolver.Target target) {
        if (snap == null) throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, "take a snapshot before using an ID selector");
        List<SelectorResolver.Node> nodes = new ArrayList<>();
        Map<Integer, Record> byId = new HashMap<>();
        for (Record record : snap.records) { nodes.add(new SelectorResolver.Node(record.id, record.testId, record.text, record.enabled)); byId.put(record.id, record); }
        SelectorResolver.Node selected = SelectorResolver.resolve(nodes, target, snap.id);
        return byId.get(selected.id);
    }

    private Rect refreshRecord(Record record) {
        if (!record.node.refresh()) throw preInputInvalidation("stale target accessibility node was invalidated");
        if (!record.node.isVisibleToUser()) throw preInputInvalidation("stale target is no longer visible");
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
        if (record.rect.isEmpty()) throw preInputInvalidation("target has no actionable bounds");
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

    private PointerCoordinates preparePointerInput(Snapshot initial, SelectorResolver.Target target) throws JSONException {
        long deadline = SystemClock.elapsedRealtime() + 2000;
        Record record = selectRecord(initial == null ? capturePointerSnapshot(deadline) : initial, target);
        SemanticIdentity originalIdentity = identity(record);
        String originalPackage = record.packageName;
        int reacquisitions = 0;
        while (true) {
            try {
                Rect bounds = stabilizeBounds(record, deadline);
                if (!SemanticPreInputRecovery.beforeDeadline(SystemClock.elapsedRealtime(), deadline))
                    throw new IllegalStateException("target preparation exceeded its two-second deadline before input");
                return new PointerCoordinates(bounds.centerX(), bounds.centerY());
            } catch (SelectorResolver.SelectorException invalidated) {
                if (!invalidated.preInputInvalidation) throw invalidated;
                long now = SystemClock.elapsedRealtime();
                if (!SemanticPreInputRecovery.canBeginReacquisition(target, reacquisitions, now, deadline)) throw invalidated;
                reacquisitions++;
                Snapshot fresh = capturePointerSnapshot(deadline);
                // Resolver failures (missing, ambiguous, disabled) are final. Never use the old record.
                record = selectRecord(fresh, target);
                if (SystemClock.elapsedRealtime() >= deadline ||
                        !SemanticPreInputRecovery.sameIdentityAndPackage(originalIdentity, originalPackage,
                                identity(record), record.packageName))
                    throw new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE,
                            "stale target semantic identity or package changed before input");
            }
        }
    }

    private Snapshot capturePointerSnapshot(long deadline) throws JSONException {
        while (SemanticPreInputRecovery.beforeDeadline(SystemClock.elapsedRealtime(), deadline)) {
            try {
                return capture(false);
            } catch (NoActiveWindowException unavailable) {
                long remaining = deadline - SystemClock.elapsedRealtime();
                if (remaining <= 0) throw unavailable;
                SystemClock.sleep(Math.min(16, remaining));
            }
        }
        throw new IllegalStateException("target preparation exceeded its two-second deadline before input");
    }

    private SemanticIdentity identity(Record record) {
        return new SemanticIdentity(record.testId, record.text, record.role, record.password);
    }

    private SelectorResolver.SelectorException preInputInvalidation(String message) {
        return new SelectorResolver.SelectorException(SelectorResolver.Failure.STALE, message, true);
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
        return stabilizeBounds(record, SystemClock.elapsedRealtime() + 2000);
    }

    private Rect stabilizeBounds(Record record, long deadline) {
        WindowGeometryStability stability = new WindowGeometryStability(lastReadyWindowId, lastReadyWindowGeneration, lastReadyWindowBounds);
        while (SystemClock.elapsedRealtime() <= deadline) {
            long generationBefore = windowEventGeneration.get();
            automation.clearCache();
            Rect nodeScreen = refreshRecord(record);
            Rect nodeWindowRect = new Rect();
            record.node.getBoundsInWindow(nodeWindowRect);
            WindowSnapshot window = findWindow(record.node.getWindowId());
            if (window == null) throw preInputInvalidation(
                    "target accessibility window is no longer available");
            long generationAfter = windowEventGeneration.get();
            long now = SystemClock.elapsedRealtime();
            boolean ready = stability.observe(window.id, generationBefore, generationAfter,
                    geometry(nodeScreen), geometry(nodeWindowRect), window.bounds,
                    window.active, window.focused, now);
            if (ready && windowEventGeneration.get() == generationAfter) {
                lastReadyWindowId = window.id;
                lastReadyWindowGeneration = generationAfter;
                lastReadyWindowBounds = window.bounds;
                return new Rect(record.rect);
            }
            SystemClock.sleep(16);
        }
        throw new IllegalStateException("target window geometry did not become stable and active/focused within 2 seconds");
    }

    private WindowSnapshot findWindow(int windowId) {
        List<android.view.accessibility.AccessibilityWindowInfo> windows = automation.getWindows();
        try {
            for (android.view.accessibility.AccessibilityWindowInfo window : windows) {
                if (window.getId() != windowId) continue;
                Rect bounds = new Rect();
                window.getBoundsInScreen(bounds);
                return new WindowSnapshot(window.getId(), geometry(bounds), window.isActive(), window.isFocused());
            }
            return null;
        } finally {
            for (android.view.accessibility.AccessibilityWindowInfo window : windows) window.recycle();
        }
    }

    private WindowGeometryStability.Bounds geometry(Rect rect) {
        return new WindowGeometryStability.Bounds(rect.left, rect.top, rect.right, rect.bottom);
    }

    private boolean awaitQuietWindow(long actionCompletedAtMs) {
        return awaitQuietWindow(actionCompletedAtMs, actionCompletedAtMs + 2000);
    }

    private boolean awaitQuietWindow(long actionCompletedAtMs, long deadline) {
        while (true) {
            long now = SystemClock.elapsedRealtime();
            long remainingOverall = deadline - now;
            if (remainingOverall <= 0) return false;
            long lastEvent = lastAccessibilityEventAtMs.get();
            long remainingQuiet = QuietWindowPolicy.remainingMs(actionCompletedAtMs, lastEvent, now, 120);
            if (remainingQuiet == 0) return true;
            synchronized (accessibilityEventMonitor) {
                if (lastAccessibilityEventAtMs.get() != lastEvent) continue;
                try { accessibilityEventMonitor.wait(Math.min(remainingQuiet, remainingOverall)); }
                catch (InterruptedException e) { Thread.currentThread().interrupt(); return false; }
            }
        }
    }

    private boolean awaitNavigation(Runnable action, long[] deadlineHolder) {
        long deadline = SystemClock.elapsedRealtime() + 5000;
        deadlineHolder[0] = deadline;
        NavigationPostcondition.State before = null;
        while (SystemClock.elapsedRealtime() < deadline) {
            before = navigationState();
            if (SystemClock.elapsedRealtime() >= deadline) return false;
            if (before != null && before.coherent && before.rootReady()) break;
            waitForNavigationSample(deadline);
        }
        if (before == null || !before.coherent || !before.rootReady() || SystemClock.elapsedRealtime() >= deadline)
            return false;

        // Invoke once. Accessibility events only wake the postcondition sampler.
        action.run();
        NavigationPostcondition postcondition = new NavigationPostcondition(before);
        while (SystemClock.elapsedRealtime() < deadline) {
            NavigationPostcondition.State candidate = navigationState();
            long now = SystemClock.elapsedRealtime();
            if (postcondition.observe(candidate, now, deadline)) return true;
            if (now >= deadline) return false;
            waitForNavigationSample(deadline);
        }
        return false;
    }

    private NavigationPostcondition.State navigationState() {
        long generationBefore = windowEventGeneration.get();
        automation.clearCache();
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null) return null;
        int rootWindowId = root.getWindowId();
        CharSequence rawPackage = root.getPackageName();
        String packageName = rawPackage == null ? null : rawPackage.toString();
        String fingerprint = semanticFingerprint(root);
        root.recycle();

        List<NavigationPostcondition.Window> windows = new ArrayList<>();
        boolean rootActive = false, rootFocused = false;
        List<android.view.accessibility.AccessibilityWindowInfo> infos = automation.getWindows();
        try {
            for (android.view.accessibility.AccessibilityWindowInfo info : infos) {
                int type = info.getType();
                if (type != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION &&
                        type != android.view.accessibility.AccessibilityWindowInfo.TYPE_INPUT_METHOD) continue;
                Rect bounds = new Rect();
                info.getBoundsInScreen(bounds);
                int policyType = type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION ?
                        NavigationPostcondition.APPLICATION : NavigationPostcondition.INPUT_METHOD;
                windows.add(new NavigationPostcondition.Window(info.getId(), policyType, info.isActive(), info.isFocused(),
                        new NavigationPostcondition.Bounds(bounds.left, bounds.top, bounds.right, bounds.bottom)));
                if (info.getId() == rootWindowId) {
                    rootActive = info.isActive();
                    rootFocused = info.isFocused();
                }
            }
        } finally {
            for (android.view.accessibility.AccessibilityWindowInfo info : infos) info.recycle();
        }
        Collections.sort(windows, Comparator.comparingInt(window -> window.id));
        long generationAfter = windowEventGeneration.get();
        return new NavigationPostcondition.State(rootWindowId, packageName, fingerprint, windows,
                rootActive, rootFocused, generationAfter, generationBefore == generationAfter);
    }

    private String semanticFingerprint(AccessibilityNodeInfo root) {
        StringBuilder fingerprint = new StringBuilder();
        int[] visited = {0};
        appendSemanticFingerprint(root, fingerprint, visited, 0);
        return fingerprint.toString();
    }

    private void appendSemanticFingerprint(AccessibilityNodeInfo node, StringBuilder out, int[] visited, int depth) {
        if (node == null || depth > MAX_DEPTH || visited[0] >= MAX_NODES) return;
        visited[0]++;
        if (node.isVisibleToUser()) {
            boolean password = node.isPassword();
            String text = NodeText.snapshotText(node.getText(), password, node.isShowingHintText());
            CharSequence description = node.getContentDescription();
            if (text.isEmpty() && !password && description != null) text = description.toString();
            String safeDescription = description == null || password ? "" : description.toString();
            CharSequence rawHint = node.getHintText();
            String safeHint = rawHint == null || password ? "" : rawHint.toString();
            CharSequence rawId = node.getViewIdResourceName();
            String id = rawId == null ? "" : rawId.toString();
            if (id.isEmpty()) {
                Bundle extras = node.getExtras();
                CharSequence tag = extras == null ? null : extras.getCharSequence("androidx.compose.ui.semantics.testTag");
                if (tag != null) id = tag.toString();
            }
            appendFingerprintField(out, id);
            appendFingerprintField(out, role(node, node.isEditable()));
            appendFingerprintField(out, text);
            appendFingerprintField(out, safeDescription);
            appendFingerprintField(out, safeHint);
            out.append(node.isEnabled() ? '1' : '0').append(node.isFocused() ? '1' : '0')
                    .append(node.isCheckable() ? '1' : '0').append(node.isChecked() ? '1' : '0')
                    .append(node.isSelected() ? '1' : '0').append(node.isClickable() ? '1' : '0')
                    .append(node.isLongClickable() ? '1' : '0').append(node.isEditable() ? '1' : '0')
                    .append(node.isScrollable() ? '1' : '0').append(password ? '1' : '0').append(';');
        }
        for (int i = 0; i < node.getChildCount() && visited[0] < MAX_NODES; i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            if (child != null) {
                try { appendSemanticFingerprint(child, out, visited, depth + 1); }
                finally { child.recycle(); }
            }
        }
    }

    private void appendFingerprintField(StringBuilder out, String value) {
        String safe = value == null ? "" : value;
        out.append(safe.length()).append(':').append(safe).append('|');
    }

    private void waitForNavigationSample(long deadline) {
        long remaining = deadline - SystemClock.elapsedRealtime();
        if (remaining <= 0) return;
        long observedEvent = lastAccessibilityEventAtMs.get();
        synchronized (accessibilityEventMonitor) {
            if (lastAccessibilityEventAtMs.get() != observedEvent) return;
            try { accessibilityEventMonitor.wait(Math.min(50, remaining)); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
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
    private static final class PointerCoordinates {
        final float x, y;
        PointerCoordinates(float x, float y) { this.x = x; this.y = y; }
    }
    private static final class NoActiveWindowException extends IllegalStateException {
        NoActiveWindowException() { super("no active accessibility window"); }
    }
    private static final class WindowSnapshot {
        final int id;
        final WindowGeometryStability.Bounds bounds;
        final boolean active, focused;
        WindowSnapshot(int id, WindowGeometryStability.Bounds bounds, boolean active, boolean focused) {
            this.id = id; this.bounds = bounds; this.active = active; this.focused = focused;
        }
    }
    private static final class Record {
        final int id; final String testId, text, role, packageName, hint, contentDescription; final Rect rect; final boolean enabled, focused, clickable, editable, password, checkable, checked, selected, scrollable, longClickable; final AccessibilityNodeInfo node;
        Record(int id, String testId, String text, String role, String packageName, Rect rect, boolean enabled, boolean focused, boolean clickable, boolean editable, boolean password, boolean checkable, boolean checked, boolean selected, boolean scrollable, boolean longClickable, String hint, String contentDescription, AccessibilityNodeInfo node) {
            this.id=id; this.testId=testId; this.text=text; this.role=role; this.packageName=packageName; this.rect=rect; this.enabled=enabled; this.focused=focused; this.clickable=clickable; this.editable=editable; this.password=password; this.checkable=checkable; this.checked=checked; this.selected=selected; this.scrollable=scrollable; this.longClickable=longClickable; this.hint=hint; this.contentDescription=contentDescription; this.node=node;
        }
    }
}
