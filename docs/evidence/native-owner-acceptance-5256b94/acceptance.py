import base64, hashlib, json, os, select, signal, socket, stat, subprocess, sys, time
from pathlib import Path
RUN=Path('/private/tmp/ashnazg-native-owner-5256b94-tkxfgvnt')
# Reuse the previously exercised project RPC/MCP assertions, not its VM launcher.
legacy=Path('/private/tmp/ashnazg-native-rust-install-618cc59-acceptance.py').read_text()
sys.argv=[str(RUN/'acceptance.py'),str(RUN),'warm']
exec(compile(legacy.split('process=None;client=None;failed=False')[0],str(RUN/'acceptance.py'),'exec'),globals())
PROBE=REPO/'target/debug/ashnazg-native-session-proof'
CONFIG=RUN/'launch.json'
config=json.loads(CONFIG.read_text())
record={'schema_version':1,'status':'starting','mode':'owned-state-restart','host_source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),'guest_source_sha':config['source_sha'],'guest_workflow_run':43807503988,'host_adb_used':False,'host_checkout_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=REPO)),'rpc_checks':[],'mcp_checks':[],'session_checks':[],'rust_native_installs':[],'proof_scope':'Explicit NativeSession API and its client factories; local unpublished pinned inputs; no acquisition or CLI/MCP app_install wiring.'}
secrets=[]
def save():
 (RUN/'acceptance.json').write_text(json.dumps(record,indent=2)+'\n')
def read_reply(timeout):
 deadline=time.monotonic()+timeout
 raw=bytearray()
 while len(raw)<4096:
  remaining=deadline-time.monotonic()
  if remaining<=0:raise RuntimeError('native owner reply deadline exceeded')
  if not select.select([process.stdout],[],[],min(remaining,1))[0]:continue
  part=os.read(process.stdout.fileno(),1)
  if not part:raise RuntimeError('native owner exited before reply')
  raw+=part
  if part==b'\n':return json.loads(raw)
 raise RuntimeError('native owner reply oversized')
def request(value,timeout=190):
 process.stdin.write(json.dumps(value,separators=(',',':')).encode()+b'\n');process.stdin.flush()
 return read_reply(timeout)
def install(credentials,path,package=None,*,bad_token=False,bad_hash=False):
 if bad_token or bad_hash:return original_install(credentials,path,package,bad_token=bad_token,bad_hash=bad_hash)
 reply=request({'op':'install','path':str(path),'package':package})
 assert reply=={'phase':'installed','launched':package is not None,'package':package},'session factory install failed'
 result={'apk_sha256':digest(path),'apk_size':path.stat().st_size,'package':package,'launched':package is not None,'client':'NativeSession::install_client / NativeInstallClient'}
 record['rust_native_installs'].append(result);save();return result
process=None;client=None;failed=False
started=time.monotonic()
state_before=STATE.stat();disk_before=(STATE/'rootfs.ext4').stat()
try:
 assert not record['host_checkout_dirty'],'proof requires a clean host checkout'
 assert config['helper_sha256']==digest(Path(config['helper']))
 assert config['agent_apk_sha256']==digest(Path(config['agent_apk']))
 assert json.loads((STATE/'runtime.json').read_text())['status']=='stopped'
 assert not os.path.lexists(STATE/'.ashnazg-session-v1.json')
 assert stat.S_IMODE(STATE.stat().st_mode)==0o700 and STATE.stat().st_uid==os.getuid()
 for port in (15555,7777,17778):
  with socket.socket() as probe:probe.settimeout(.5);assert probe.connect_ex(('127.0.0.1',port))!=0,'requested listener occupied'
 signature=subprocess.run(['codesign','--verify','--strict',config['helper']],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20)
 assert signature.returncode==0,'staged helper signature validation failed'
 record['staged_adhoc_signature_verified']=True
 record['config_sha256']=digest(CONFIG)
 record['helper_sha256']=config['helper_sha256'];record['agent_apk_sha256']=config['agent_apk_sha256']
 record['host_binary_sha256']=digest(HOST);record['probe_sha256']=digest(PROBE)
 record['probe_source_sha256']=digest(RUN/'probe/src/main.rs');record['driver_sha256']=digest(Path(__file__))
 record['preflight']=json.loads((BASE/'preflight-verification.json').read_text())
 record['state_disk_before']={'dev':disk_before.st_dev,'inode':disk_before.st_ino,'size':disk_before.st_size}
 env={**os.environ,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','ASHNAZG_ADB':'/host-adb-disabled','ASHNAZG_CI_DIAGNOSTICS':'1'}
 env.pop('ASHNAZG_TOKEN',None);env.pop('ASHNAZG_AGENT_ADDR',None)
 with (RUN/'owner.stderr').open('wb') as errors:
  process=subprocess.Popen([str(PROBE),str(CONFIG)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,bufsize=0,env=env)
  record['owner_pid']=process.pid;save()
  print('Starting owned native session through Rust API',flush=True)
  ready=read_reply(640)
  assert ready.get('phase')=='ready' and ready['api_level']>=34
  record['ready']=ready;record['observed_ready_seconds']=round(time.monotonic()-started,3)
  record['ready_runtime_receipt']=json.loads((STATE/'runtime.json').read_text())
  credential=STATE/'.ashnazg-session-v1.json'
  assert stat.S_IMODE(credential.stat().st_mode)==0o600
  credentials=json.loads(credential.read_text());secrets=[credentials['rpc_token'],credentials['install_token']]
  assert credentials['helper_pid']==ready['helper_pid'] and ready['helper_pid']!=process.pid
  assert credentials['session_id']==ready['session_id'] and credentials['manifest_source_sha']==config['source_sha']
  assert record['ready_runtime_receipt']['session_id']==ready['session_id']
  record['session_checks']+=['retained_child_identity_matches_credentials','authenticated_factory_ping','separate_owner_and_helper_processes']
  save();print('Owner ready; running Rust factory installs and project assertions',flush=True)
  install(credentials,FIXTURE,bad_token=True);record['session_checks'].append('python_protocol_invalid_auth_rejected_before_payload')
  install(credentials,FIXTURE,bad_hash=True);record['session_checks'].append('python_protocol_corrupt_upload_rejected')
  install(credentials,FIXTURE,'dev.ashnazg.fixture')
  client=checks.RpcClient(credentials['rpc_token'],timeout=30)
  checks.wait_for_framework_ready(client,'dev.ashnazg.fixture:id/increment','Count: 0')
  record['rpc_checks']=checks.verify_rpc(client,RUN/'rpc')
  record['rpc_checks']+=framework(client,credentials,'compose','com.ashnazg.fixture.compose','compose-button','Compose count: 0','Compose count: 1')
  record['rpc_checks']+=framework(client,credentials,'reactnative','com.ashnazg.fixture.reactnative','rn-button','React Native count: 0','React Native count: 1')
  record['mcp_checks']=mcp(credentials)
  record['mcp_checks']+=checks.verify_http_auth(str(HOST),SimpleNamespace(token=credentials['rpc_token'],serial='native-owner-no-host-adb'))
  client.close();client=None
  print('Assertions passed; requesting owner shutdown',flush=True)
  stopped=request({'op':'shutdown'},timeout=235)
  assert stopped=={'phase':'stopped','code':0,'signal':None,'forced':False,'credential_absent':True,'parent_cancelled':False}
  record['owner_shutdown_reply']=stopped
  process.wait(timeout=10);assert process.returncode==0
  record['status']='tests_passed';save()
except BaseException as error:
 failed=True;record['error_type']=type(error).__name__
 detail=str(error)[:2000]
 for secret in secrets:detail=detail.replace(secret,'<redacted>')
 record['error']=detail;save();print('Owned native acceptance failed: '+record['error_type'],flush=True)
finally:
 if client:client.close()
 if process and process.poll() is None:
  # The test owner handles TERM during startup as well as during interaction.
  process.terminate()
  try:process.wait(timeout=235)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10);failed=True;record['forced_owner_exit']=True
 if process:record['owner_exit_code']=process.returncode
 record['final_runtime_receipt']=json.loads((STATE/'runtime.json').read_text())
 record['credential_removed']=not os.path.lexists(STATE/'.ashnazg-session-v1.json')
 record['owned_listeners_closed']={}
 for port in (15555,7777,17778):
  with socket.socket() as probe:probe.settimeout(.5);record['owned_listeners_closed'][str(port)]=probe.connect_ex(('127.0.0.1',port))!=0
 disk_after=(STATE/'rootfs.ext4').stat()
 record['state_disk_identity_preserved']=(disk_before.st_dev,disk_before.st_ino,disk_before.st_size)==(disk_after.st_dev,disk_after.st_ino,disk_after.st_size)
 record['state_directory_identity_preserved']=(state_before.st_dev,state_before.st_ino)==(STATE.stat().st_dev,STATE.stat().st_ino)
 record['helper_reaped']=not (RUN/'helper-alive').exists()
 if 'ready' in record:
  try:os.kill(record['ready']['helper_pid'],0);record['helper_reaped']=False
  except ProcessLookupError:record['helper_reaped']=True
 # Retain bounded output with credentials redacted; never retain the credential file.
 for name,source in [('serial.log',STATE/'serial.log'),('runtime.json',STATE/'runtime.json')]:
  raw=source.read_bytes();assert len(raw)<=32*1024*1024
  for secret in secrets:raw=raw.replace(secret.encode(),b'<redacted>')
  (RUN/name).write_bytes(raw)
 for path in RUN.rglob('*'):
  if not path.is_file() or path.stat().st_size>32*1024*1024 or path.name in ('ashnazg-vm','agent.apk'):continue
  raw=path.read_bytes()
  if any(secret.encode() in raw for secret in secrets):failed=True;record['secret_in_retained_output']=True
 if record.get('owner_exit_code')!=0 or record['final_runtime_receipt']['status']!='stopped' or not record['credential_removed'] or not all(record['owned_listeners_closed'].values()) or not record['helper_reaped']:failed=True
 record['status']='failed' if failed else 'passed';record['total_observed_seconds']=round(time.monotonic()-started,3);save()
 print(json.dumps({'status':record['status'],'rpc_checks':len(record['rpc_checks']),'mcp_checks':len(record['mcp_checks']),'rust_installs':len(record['rust_native_installs']),'run':str(RUN)}),flush=True)
sys.exit(1 if failed else 0)
