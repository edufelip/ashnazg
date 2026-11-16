#!/usr/bin/env python3
"""Owned native MCP acceptance. No host SDK, ADB, aapt or shell commands."""
import argparse, base64, gzip, hashlib, json, os, re, select, shutil, signal, socket, stat, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = json.loads((HERE / "build-receipt.json").read_text())
CLIENT_BYTES = (HERE / "mcp_http.py").read_bytes()
assert hashlib.sha256(CLIENT_BYTES).hexdigest() == BUILD["files"]["mcp_http.py"]["sha256"]
CLIENT = {"__file__":str(HERE / "mcp_http.py"),"__name__":"bound_mcp_http"}
exec(compile(CLIENT_BYTES, str(HERE / "mcp_http.py"), "exec"), CLIENT)
McpHttpClient, ProofFailure, structured, screenshot = [CLIENT[key] for key in ("McpHttpClient","ProofFailure","structured","screenshot")]

APKS = [
 ("native","agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk","dev.ashnazg.fixture","dev.ashnazg.fixture:id/increment","dev.ashnazg.fixture:id/input","Count: ",None),
 ("compose","fixtures/compose/build/outputs/apk/debug/compose-debug.apk","com.ashnazg.fixture.compose","compose-button","compose-input","Compose count: ","com.ashnazg.fixture.compose"),
 ("reactnative","fixtures/reactnative/build/outputs/apk/debug/reactnative-debug.apk","com.ashnazg.fixture.reactnative","rn-button","rn-input","React Native count: ",None),
]


def digest(path):
    h = hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()


def closed(port):
    try:
        with socket.create_connection(("127.0.0.1",port),timeout=0.3): return False
    except OSError: return True


def identity(path):
    s = path.stat()
    return {"device":s.st_dev,"inode":s.st_ino,"uid":s.st_uid,"mode":stat.S_IMODE(s.st_mode),"size":s.st_size}


def process_absent(pid):
    try: os.kill(pid,0); return False
    except ProcessLookupError: return True
    except PermissionError: return False


def await_line(process,seconds):
    if not select.select([process.stdout],[],[],seconds)[0]: raise ProofFailure("probe control deadline expired")
    line = process.stdout.readline(8193)
    if not line or len(line)>8192: raise ProofFailure("invalid probe control frame")
    try: return json.loads(line)
    except ValueError: raise ProofFailure("invalid probe control JSON") from None


def main():
    a=argparse.ArgumentParser(); a.add_argument("--config",required=True); a.add_argument("--binary",required=True); a.add_argument("--apk-root",required=True); a.add_argument("--out",required=True); a.add_argument("--stop",choices=["marker","sigterm"],default="marker"); args=a.parse_args()
    config_path=Path(args.config).resolve(); c=json.loads(config_path.read_text()); out=Path(args.out).resolve(); out.mkdir(mode=0o700); apk_root=Path(args.apk_root).resolve(); binary=Path(args.binary).resolve()
    if c["host_source_sha"] != BUILD["host_source_sha"] or digest(binary)!=BUILD["binary_sha256"]: raise ProofFailure("probe build binding mismatch")
    for source,name in [(HERE/"acceptance.py","acceptance.py"),(HERE/"mcp_http.py","mcp_http.py"),(HERE/"build-receipt.json","build-receipt.json"),(HERE/"preflight.json","preflight.json"),(HERE/"probe/src/main.rs","probe-source.rs"),(HERE/"probe/Cargo.toml","probe-Cargo.toml"),(HERE/"probe/Cargo.lock","probe-Cargo.lock")]:
        shutil.copyfile(source,out/name)
    marker=Path(c["shutdown_marker"]); state=Path(c["state_dir"]); credential=state/".ashnazg-session-v1.json"; disk=state/"rootfs.ext4"
    ports=[c[key] for key in ("adb_port","agent_port","install_port","http_port")]
    if marker.exists() or credential.exists() or not all(closed(port) for port in ports): raise ProofFailure("owned resources are not initially available")
    before={"state":identity(state),"disk":identity(disk)}
    secrets=[c["http_bearer"].encode()]
    checks=[]; records=[]; process=None; ready=None; stopped=None; failure=None; started=time.monotonic(); tool_calls=0
    def no_secrets(data):
        if any(value and value in data for value in secrets): raise ProofFailure("retained evidence contains a credential")
    def save_json(name,value):
        raw=(json.dumps(value,indent=2,ensure_ascii=False)+"\n").encode(); no_secrets(raw); (out/name).write_bytes(raw)
    def record(name,value):
        copy=json.loads(json.dumps(value))
        for item in copy.get("result",{}).get("content",[]):
            if item.get("type")=="image" and "data" in item:
                image=base64.b64decode(item.pop("data"),validate=True); item["data_sha256"]=hashlib.sha256(image).hexdigest(); item["decoded_bytes"]=len(image)
        save_json("%03d-%s.json"%(len(records)+1,name),copy); records.append(name)
    def tool(name,arguments,allow_error=False):
        nonlocal tool_calls
        result=client.tool(name,arguments,allow_error=allow_error); tool_calls+=1; record(name,result); return result
    def snap(): return structured(tool("ui_snapshot",{}))
    def nodes(s):
        if not isinstance(s.get("nodes"),list): raise ProofFailure("snapshot omitted nodes")
        return s["nodes"]
    def find(s,key,value):
        found=[n for n in nodes(s) if n.get(key)==value]
        if len(found)!=1: raise ProofFailure("fixture node was missing or ambiguous")
        return found[0]
    def count(s,prefix):
        values=set()
        for n in nodes(s):
            for field in ("text","contentDescription"):
                text=n.get(field)
                if isinstance(text,str) and re.fullmatch(re.escape(prefix)+r"\d+",text): values.add(int(text[len(prefix):]))
        if len(values)!=1: raise ProofFailure("fixture count state unavailable")
        return next(iter(values))
    def act(arguments):
        value=structured(tool("ui_act",arguments))
        if value.get("settled") is not True: raise ProofFailure("action did not settle")
        result=value.get("snapshot",{}); nodes(result); return result
    def wait_fixture(button,prefix):
        end=time.monotonic()+15
        while time.monotonic()<end:
            value=snap()
            if any(n.get("testId")==button and n.get("enabled") for n in nodes(value)):
                try: count(value,prefix); return value
                except ProofFailure: pass
            time.sleep(0.2)
        raise ProofFailure("fixture did not become ready")
    stderr_path=out/"probe.stderr"
    env={key:os.environ[key] for key in ("HOME","USER","LOGNAME","TMPDIR","LANG","HTTP_PROXY","HTTPS_PROXY","NO_PROXY","http_proxy","https_proxy","no_proxy") if key in os.environ}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin",ASHNAZG_ADB="/nonexistent-host-adb",AAPT="/nonexistent-host-aapt",RUST_BACKTRACE="0")
    save_json("environment-contract.json",{"PATH":env["PATH"],"ASHNAZG_ADB":env["ASHNAZG_ADB"],"AAPT":env["AAPT"],"ASHNAZG_TOKEN_present":"ASHNAZG_TOKEN" in env,"ANDROID_SERIAL_present":"ANDROID_SERIAL" in env,"adb_available":shutil.which("adb",path=env["PATH"]) is not None,"aapt_available":shutil.which("aapt",path=env["PATH"]) is not None})
    try:
        with stderr_path.open("wb") as error:
            process=subprocess.Popen([str(binary),str(config_path)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=error,env=env)
        ready=await_line(process,c["startup_timeout_secs"]+15)
        if ready.get("status")!="ready" or ready.get("host_source_sha")!=BUILD["host_source_sha"]: raise ProofFailure("probe did not report bound readiness")
        save_json("ready.json",ready)
        if ready.get("adb_listen")!="127.0.0.1:%s"%c["adb_port"]: raise ProofFailure("owned serial metadata mismatch")
        cs=credential.lstat()
        if not stat.S_ISREG(cs.st_mode) or stat.S_IMODE(cs.st_mode)!=0o600 or cs.st_uid!=os.getuid() or cs.st_size>8192: raise ProofFailure("invalid credential scan boundary")
        creds=json.loads(credential.read_bytes())
        if creds.get("session_id")!=ready.get("session_id") or creds.get("helper_pid")!=ready.get("helper_pid"): raise ProofFailure("credential scan identity mismatch")
        for field in ("rpc_token","install_token"):
            value=creds.get(field)
            if not isinstance(value,str) or not re.fullmatch(r"[0-9a-f]{64}",value): raise ProofFailure("credential scan token shape mismatch")
            secrets.append(value.encode())
        client=McpHttpClient(ready["http_listen"],c["http_bearer"]); record("initialize",client.initialize()); checks.append("http_auth_and_initialize")
        _, listed=client.request("tools/list",{}); record("tools-list",listed)
        if sorted(t.get("name","") for t in listed.get("result",{}).get("tools",[]))!=["app_install","app_logs","ui_act","ui_screenshot","ui_snapshot"]: raise ProofFailure("tool advertisement changed")
        checks.append("five_tool_contract")
        for name,relative,package,button,input_id,prefix,explicit in APKS:
            print(json.dumps({"phase":"install","fixture":name}),flush=True)
            arguments={"path":str(apk_root/relative),"launch":True}
            if explicit: arguments["package"]=explicit
            value=structured(tool("app_install",arguments))
            if value!={"installed":True,"package":package,"serial":ready["adb_listen"]}: raise ProofFailure("native installation result contract mismatch")
            checks.append(name+"_native_mcp_install_"+("explicit" if explicit else "inferred"))
            initial=wait_fixture(button,prefix); old=count(initial,prefix)
            changed=act({"action":"tap","target":{"testId":button}})
            if count(changed,prefix)!=old+1: raise ProofFailure("fixture tap did not update count")
            checks.append(name+"_tap_and_settled_snapshot")
            act({"action":"clear","target":{"testId":input_id}})
            typed=act({"action":"type","target":{"testId":input_id},"text":"Native MCP 你好"})
            if find(typed,"testId",input_id).get("text")!="Native MCP 你好": raise ProofFailure("fixture Unicode type failed")
            cleared=act({"action":"clear","target":{"testId":input_id}})
            if find(cleared,"testId",input_id).get("text","")!="": raise ProofFailure("fixture clear failed")
            checks.append(name+"_unicode_type_and_clear")
            image,meta=screenshot(tool("ui_screenshot",{"format":"png","somOverlay":True}))
            if not isinstance(meta.get("snapshotId"),int) or not meta.get("marks") or meta.get("width",0)<=0 or meta.get("height",0)<=0: raise ProofFailure("screenshot metadata failed")
            no_secrets(image); (out/(name+".png")).write_bytes(image); save_json(name+"-screenshot.json",meta); checks.append(name+"_png_and_semantic_marks")
        value=structured(tool("app_install",{"path":str(apk_root/APKS[0][1]),"launch":False}))
        if value!={"installed":True,"package":None,"serial":ready["adb_listen"]}: raise ProofFailure("no-launch result contract mismatch")
        find(snap(),"testId","rn-button"); checks.append("native_install_without_launch_preserves_foreground")
        for arguments in ({"path":str(apk_root/APKS[0][1]),"launch":False,"package":"dev.ashnazg.fixture"},{"path":str(out/"malformed.apk"),"launch":True}):
            (out/"malformed.apk").write_bytes(b"private-negative-payload-sentinel")
            value=tool("app_install",arguments,allow_error=True)
            if "error" not in value and not value.get("result",{}).get("isError"): raise ProofFailure("invalid installation request was accepted")
        checks.append("invalid_parameters_and_malformed_apk_rejected")
        relaunched=structured(tool("app_install",{"path":str(apk_root/APKS[0][1]),"launch":True,"package":"dev.ashnazg.fixture"}))
        if relaunched!={"installed":True,"package":"dev.ashnazg.fixture","serial":ready["adb_listen"]}: raise ProofFailure("native relaunch result failed")
        wait_fixture(APKS[0][3],APKS[0][5]); checks.append("native_relaunch_before_navigation")
        linked=act({"action":"deep_link","text":"ashnazg://fixture"})
        if not any("Deep link" in str(n.get("text","")) for n in nodes(linked)): raise ProofFailure("deep link failed")
        find(act({"action":"back"}),"testId","dev.ashnazg.fixture:id/increment"); checks.append("native_mcp_deep_link_and_back")
        logs=structured(tool("app_logs",{"tagFilter":"AshnazgAgent","sinceMs":0}))
        if not isinstance(logs.get("lines"),list) or not isinstance(logs.get("truncated"),bool): raise ProofFailure("log contract failed")
        checks.append("native_mcp_app_logs")
    except Exception as error:
        failure=type(error).__name__
        diagnostic={"error_class":failure,"message":str(error) if isinstance(error,ProofFailure) else None,"last_response":getattr(locals().get("client"),"last_response",None)}
        try: save_json("failure-diagnostic.json",diagnostic)
        except ProofFailure: save_json("failure-diagnostic.json",{"error_class":failure,"details_redacted":True})
        print(json.dumps({"phase":"failed","error_class":failure,"message":diagnostic["message"]}),flush=True)
    finally:
        if process is not None:
            if process.poll() is None:
                if args.stop=="sigterm": process.send_signal(signal.SIGTERM)
                else: marker.write_text("stop\n")
            try:
                stopped=await_line(process,250)
                process.wait(timeout=10)
            except Exception:
                failure=failure or "ProbeShutdownFailure"
            if process.poll() is None: raise ProofFailure("owned probe remains running; manual inspection required")
    if stopped: save_json("stopped.json",stopped)
    after={"state":identity(state),"disk":identity(disk)}
    cleanup={"probe_exit_code":process.returncode if process else None,"helper_absent":process_absent(ready["helper_pid"]) if ready and ready.get("helper_pid") else None,"credential_absent":not os.path.lexists(credential),"listeners_closed":{str(p):closed(p) for p in ports},"state_identity_preserved":before["state"]["device"]==after["state"]["device"] and before["state"]["inode"]==after["state"]["inode"],"disk_identity_preserved":before["disk"]["device"]==after["disk"]["device"] and before["disk"]["inode"]==after["disk"]["inode"],"disk_bytes_unchanged_claimed":False}
    if not stopped or stopped.get("status")!="stopped" or stopped.get("serving_and_cleanup_ok") is not True or stopped.get("parent_cancelled") or process.returncode!=0 or not cleanup["helper_absent"] or not cleanup["credential_absent"] or not all(cleanup["listeners_closed"].values()) or not cleanup["state_identity_preserved"] or not cleanup["disk_identity_preserved"]: failure=failure or "CleanupContractFailure"
    save_json("cleanup.json",cleanup)
    for filename in ("runtime.json","serial.log"):
        source=state/filename
        if source.exists():
            raw=source.read_bytes()
            if len(raw)>16*1024*1024: raise ProofFailure("runtime evidence exceeded cap")
            no_secrets(raw)
            if filename.endswith(".log"):
                (out/(filename+".gz")).write_bytes(gzip.compress(raw,mtime=0)); save_json("serial-uncompressed.json",{"size":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
            else: (out/filename).write_bytes(raw)
    no_secrets(stderr_path.read_bytes())
    receipt={"schema_version":1,"status":"passed" if failure is None else "failed","failure_class":failure,"host_source_sha":BUILD["host_source_sha"],"stop_mode":args.stop,"checks":checks,"tool_calls":tool_calls,"elapsed_seconds":round(time.monotonic()-started,3),"credential_values_scanned":len(secrets),"credential_matches_found":0,"scope":"Explicit native library MCP HTTP session with existing guest; no CLI or acquisition proof","inputs":[{"relative_path":relative,"size":(apk_root/relative).stat().st_size,"sha256":digest(apk_root/relative)} for _,relative,*_ in APKS],"payloads":{}}
    for file in sorted(out.iterdir()):
        if file.is_file(): receipt["payloads"][file.name]={"size":file.stat().st_size,"sha256":digest(file)}
    save_json("receipt.json",receipt)
    print(json.dumps({"phase":"finished","status":receipt["status"],"checks":len(checks),"tool_calls":tool_calls,"out":str(out)}),flush=True)
    return 0 if failure is None else 2

if __name__=="__main__":
    try: raise SystemExit(main())
    except ProofFailure:
        print(json.dumps({"phase":"driver-failure"}),flush=True); raise SystemExit(2)
