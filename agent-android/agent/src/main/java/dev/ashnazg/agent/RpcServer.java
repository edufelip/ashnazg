package dev.ashnazg.agent;

import android.app.Instrumentation;
import android.app.UiAutomation;
import android.os.Build;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.BufferedInputStream;
import java.io.Closeable;
import java.io.IOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

final class RpcServer implements Closeable {
    private static final String TAG = "AshnazgAgent";
    private static final int MAX_MESSAGE = 8 * 1024 * 1024;
    private static final int IDLE_TIMEOUT_MS = 120000;
    private static final int FRAME_TIMEOUT_SECONDS = 30;
    private final String token;
    private final UiController ui;
    private final ServerSocket listener;
    private final AtomicBoolean closed = new AtomicBoolean();
    private final ExecutorService clients = new ThreadPoolExecutor(4, 4, 0L, TimeUnit.MILLISECONDS, new ArrayBlockingQueue<>(4));
    private final ScheduledExecutorService deadlines = Executors.newSingleThreadScheduledExecutor(r -> { Thread t = new Thread(r, "ashnazg-rpc-deadlines"); t.setDaemon(true); return t; });
    private final Set<Socket> activeSockets = ConcurrentHashMap.newKeySet();

    RpcServer(String token, UiAutomation automation, Instrumentation instrumentation) throws IOException {
        this.token = token;
        this.ui = new UiController(automation, instrumentation, token);
        listener = new ServerSocket(7777, 4, InetAddress.getByName("127.0.0.1"));
    }

    void run() throws IOException {
        while (!closed.get()) {
            try {
                Socket socket = listener.accept();
                socket.setSoTimeout(IDLE_TIMEOUT_MS);
                activeSockets.add(socket);
                try { clients.execute(() -> serve(socket)); }
                catch (RejectedExecutionException e) { activeSockets.remove(socket); try { socket.close(); } catch (IOException ignored) { } }
            } catch (IOException e) { if (!closed.get()) Log.w(TAG, "RPC connection ended: " + e.getMessage()); }
        }
    }

    private void serve(Socket socket) {
        try (Socket connection = socket; BufferedInputStream input = new BufferedInputStream(connection.getInputStream())) {
            while (!closed.get()) {
                AtomicReference<ScheduledFuture<?>> frameDeadline = new AtomicReference<>();
                String line;
                try {
                    line = RpcFrameReader.readLine(input, MAX_MESSAGE, () -> frameDeadline.set(deadlines.schedule(() -> {
                        try { connection.close(); } catch (IOException ignored) { }
                    }, FRAME_TIMEOUT_SECONDS, TimeUnit.SECONDS)));
                } finally {
                    ScheduledFuture<?> deadline = frameDeadline.get();
                    if (deadline != null) deadline.cancel(false);
                }
                if (line == null) break;
                JSONObject response = dispatch(line);
                if (response != null) {
                    byte[] bytes = (response.toString() + "\n").getBytes(StandardCharsets.UTF_8);
                    if (bytes.length > MAX_MESSAGE) bytes = (error(response.opt("id"), -32000, "response exceeds 8 MiB").toString() + "\n").getBytes(StandardCharsets.UTF_8);
                    connection.getOutputStream().write(bytes);
                    connection.getOutputStream().flush();
                }
            }
        } catch (IOException e) { if (!closed.get()) Log.w(TAG, "RPC connection ended: " + e.getMessage()); }
        finally { activeSockets.remove(socket); }
    }

    JSONObject dispatch(String raw) {
        Object id = JSONObject.NULL;
        boolean hasId = false;
        try {
            JSONObject request = new JSONObject(raw);
            id = request.opt("id");
            hasId = request.has("id");
            if (hasId && !validId(id)) return error(JSONObject.NULL, -32600, "invalid request id");
            if (!(request.opt("jsonrpc") instanceof String) || !"2.0".equals(request.optString("jsonrpc"))) return hasId ? error(id, -32600, "invalid JSON-RPC request") : null;
            if (!(request.opt("token") instanceof String) || !token.equals(request.optString("token"))) return hasId ? error(id, -32001, "unauthenticated") : null;
            if (!(request.opt("method") instanceof String) || request.optString("method").isEmpty()) return hasId ? error(id, -32600, "method is required") : null;
            String method = request.optString("method");
            Object rawParams = request.opt("params");
            if (request.has("params") && !(rawParams instanceof JSONObject)) return hasId ? error(id, -32602, "params must be an object") : null;
            JSONObject params = rawParams instanceof JSONObject ? (JSONObject) rawParams : new JSONObject();
            try {
                JSONObject result;
                synchronized (ui) { result = invoke(method, params); }
                return hasId ? success(id, result) : null;
            } catch (UnknownMethod e) { return hasId ? error(id, -32601, e.getMessage()) : null; }
            catch (BadParams e) { return hasId ? error(id, -32602, e.getMessage()) : null; }
            catch (IllegalArgumentException | SelectorResolver.SelectorException e) { return hasId ? error(id, -32602, e.getMessage()) : null; }
            catch (Exception e) { Log.e(TAG, "RPC operation failed: " + method, e); return hasId ? error(id, -32000, safeMessage(e)) : null; }
        } catch (JSONException e) { return error(id, -32700, "parse error"); }
    }

    private JSONObject invoke(String method, JSONObject params) throws Exception {
        switch (method) {
            case "ping": return new JSONObject().put("version", "0.1.0").put("apiLevel", Build.VERSION.SDK_INT);
            case "ui_snapshot": return ui.snapshot(ui.booleanParam(params, "includeInvisible", false));
            case "ui_act": return ui.act(params);
            case "ui_screenshot": return ui.screenshot(params);
            case "app_logs": return ui.logs(params);
            default: throw new UnknownMethod("unknown method: " + method);
        }
    }
    private JSONObject success(Object id, JSONObject result) throws JSONException { return new JSONObject().put("jsonrpc", "2.0").put("id", id).put("result", result); }
    private JSONObject error(Object id, int code, String message) {
        try { return new JSONObject().put("jsonrpc", "2.0").put("id", id).put("error", new JSONObject().put("code", code).put("message", message)); }
        catch (JSONException ignored) { return new JSONObject(); }
    }
    private String safeMessage(Exception e) { String m = e.getMessage(); return m == null ? "operation failed" : m; }
    private boolean validId(Object id) {
        if (id == JSONObject.NULL || id instanceof String) return true;
        if (id instanceof Integer || id instanceof Long) return true;
        if (id instanceof Double) { double n = (Double) id; return Double.isFinite(n) && n == Math.rint(n); }
        return false;
    }
    @Override public void close() {
        if (closed.compareAndSet(false, true)) {
            for (Socket socket : activeSockets) try { socket.close(); } catch (IOException ignored) { }
            clients.shutdownNow(); deadlines.shutdownNow(); ui.close();
            try { listener.close(); } catch (IOException ignored) { }
        }
    }
    private static final class UnknownMethod extends Exception { UnknownMethod(String m) { super(m); } }
    private static final class BadParams extends Exception { BadParams(String m) { super(m); } }
}
