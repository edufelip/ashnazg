import base64, hashlib, json, os, signal, socket, stat, subprocess, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

REPO=Path('/Users/eduwaldo/.t3/worktrees/ashnazg/t3-a04e62d5-118037da6f6e517d')
sys.path.insert(0,str(REPO))
from scripts import integration as checks
from runtime.microvm.kernel_config import validate_config_files

BASE=Path('/private/tmp/ashnazg-native-session-b8806d8-guest')
ASSETS=BASE/'assets'
APKS=Path('/private/tmp/ashnazg-native-session-b8806d8-apks')
HELPER=REPO/'platform/macos/.build/release/ashnazg-vm'
HOST=REPO/'target/debug/ashnazg'
AGENT=APKS/'agent-android/agent/build/outputs/apk/debug/agent-debug.apk'
FIXTURE=APKS/'agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk'
RUN=Path(sys.argv[1]) if len(sys.argv)>1 else Path(tempfile.mkdtemp(prefix='ashnazg-native-session-acceptance-',dir='/private/tmp'))
MODE=sys.argv[2] if len(sys.argv)>2 else 'cold'
assert MODE in ('cold','warm')
STATE=RUN/'state'
record={'schema_version':1,'mode':MODE,'status':'starting','source_sha':'b8806d85953561ca1c7ccdf88cb5093fec7a1c9a','host_source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),'workflow_run':43802590487,'host_adb_used':False,'rpc_checks':[],'mcp_checks':[],'session_checks':[]}
record['host_checkout_dirty']=bool(subprocess.check_output(['git','status','--porcelain'],cwd=REPO))
secrets=[]
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(65536),b''):h.update(b)
 return h.hexdigest()
def save(): (RUN/(MODE+'-acceptance.json')).write_text(json.dumps(record,indent=2)+'\n')
def line(sock,limit=4096):
 raw=bytearray()
 while len(raw)<limit:
  b=sock.recv(1)
  if not b:raise RuntimeError('protocol EOF')
  raw+=b
  if b==b'\n':return json.loads(raw)
 raise RuntimeError('protocol response too large')
def install(credentials,path,package=None,*,bad_token=False,bad_hash=False):
 header={'schema_version':1,'op':'install','session_id':credentials['session_id'],'install_token':'0'*64 if bad_token else credentials['install_token'],'apk_size':path.stat().st_size,'apk_sha256':'0'*64 if bad_hash else digest(path),'launch':package is not None}
 if package:header['package']=package
 host,port=credentials['install_listen'].split(':')
 with socket.create_connection((host,int(port)),timeout=15) as connection:
  connection.settimeout(30)
  connection.sendall(json.dumps(header,separators=(',',':')).encode()+b'\n')
  ack=line(connection)
  if bad_token:
   assert ack.get('status')!='ready_for_payload' and 'error' in ack
   return {'rejected_before_payload':True}
  assert ack=={'schema_version':1,'status':'ready_for_payload'},'missing upload ACK'
  with path.open('rb') as source:
   for block in iter(lambda:source.read(65536),b''):connection.sendall(block)
  connection.shutdown(socket.SHUT_WR)
  connection.settimeout(180)
  result=line(connection)
  safe_reply={key:result[key] for key in ('schema_version','status','error','session_id','launched') if key in result and isinstance(result[key],(str,int,bool))}
  for key in ('status','error'):
   if key in safe_reply and safe_reply[key] not in ('installed','error','unauthorized','busy','closed','timeout','invalid_request','invalid_upload','backend_failed','backend_unavailable'):safe_reply[key]='unrecognized'
  record.setdefault('install_replies',[]).append(safe_reply);save()
  if bad_hash:
   assert result.get('status')!='installed' and 'error' in result
   return {'corrupt_upload_rejected':True}
  assert result=={'schema_version':1,'status':'installed','session_id':credentials['session_id'],'launched':package is not None},'install or launch did not succeed'
  return {'apk_sha256':header['apk_sha256'],'apk_size':header['apk_size'],'package':package,'launched':package is not None}
def framework(client,credentials,name,package,button,initial,updated):
 path=APKS/f'fixtures/{name}/build/outputs/apk/debug/{name}-debug.apk'
 record.setdefault('installs',[]).append(install(credentials,path,package))
 checks.wait_for_framework_ready(client,button,initial)
 changed=checks.act(client,{'action':'tap','target':{'testId':button}})
 checks.assert_button_label(changed,button,updated)
 field='compose-input' if name=='compose' else 'rn-input'
 checks.act(client,{'action':'type','target':{'testId':field},'text':'Framework 你好'})
 checks.assert_field_text(client.call('ui_snapshot'),field,'Framework 你好')
 checks.act(client,{'action':'clear','target':{'testId':field}})
 checks.assert_field_text(client.call('ui_snapshot'),field,'')
 image=client.call('ui_screenshot',{'format':'png','somOverlay':True})
 png=base64.b64decode(image['data'],validate=True)
 assert image['mimeType']=='image/png' and png.startswith(b'\x89PNG\r\n\x1a\n')
 (RUN/(MODE+'-'+name+'.png')).write_bytes(png)
 return [name+'_'+part for part in ('semantic_id','tap_count','unicode_type_clear','screenshot')]
def mcp(credentials):
 env={**os.environ,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','ASHNAZG_ADB':'/host-adb-disabled','ASHNAZG_TOKEN':credentials['rpc_token'],'ASHNAZG_AGENT_ADDR':credentials['agent_listen']}
 with (RUN/(MODE+'-mcp.stderr')).open('w') as errors:
  p=subprocess.Popen([str(HOST),'mcp'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,text=True,bufsize=1,env=env)
  try:
   response=checks.mcp_send(p,{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'native-session-acceptance','version':'1'}}},1)
   assert response.get('result',{}).get('protocolVersion')
   checks.mcp_send(p,{'jsonrpc':'2.0','method':'notifications/initialized'})
   response=checks.mcp_send(p,{'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},2)
   assert {'ui_snapshot','ui_act','ui_screenshot','app_logs','app_install'}<={t['name'] for t in response['result']['tools']}
   cases=[('ui_snapshot',{}),('ui_act',{'action':'deep_link','text':'ashnazg://fixture'}),('ui_act',{'action':'back'}),('app_logs',{'tagFilter':'AshnazgAgent','sinceMs':0}),('ui_screenshot',{'format':'png'})]
   for n,(name,arguments) in enumerate(cases,3):
    response=checks.mcp_send(p,{'jsonrpc':'2.0','id':n,'method':'tools/call','params':{'name':name,'arguments':arguments}},n)
    assert not response.get('result',{}).get('isError'), 'MCP tool failed'
    if name=='ui_screenshot':
     images=[x for x in response['result']['content'] if x.get('type')=='image' and x.get('mimeType')=='image/png']
     assert images
     (RUN/(MODE+'-mcp.png')).write_bytes(base64.b64decode(images[0]['data'],validate=True))
   return ['initialize_and_list','snapshot','deep_link_and_back','logs','screenshot']
  finally:
   if p.stdin:p.stdin.close()
   try:p.wait(timeout=8)
   except subprocess.TimeoutExpired:p.terminate();p.wait(timeout=8)
   record['mcp_exit_code']=p.returncode
process=None;client=None;failed=False
RUN.mkdir(mode=0o700,exist_ok=True)
record['agent_apk_sha256']=digest(AGENT)
record['helper_sha256']=digest(HELPER)
record['host_binary_sha256']=digest(HOST)
record['driver_sha256']=digest(Path(__file__))
record['helper_and_integration_source_match_guest']=not subprocess.check_output(['git','diff',record['source_sha'],'--','platform/macos','scripts/integration.py'],cwd=REPO)
record['preflight']=json.loads((BASE/'preflight-verification.json').read_text())
validate_config_files(ASSETS/'kernel.config',ASSETS/'kernel.generated.config')
record['preflight']['all_fragment_settings_verified']=True
args=[str(HELPER),'--assets',str(ASSETS),'--state-dir',str(STATE),'--adb-port','15555','--agent-port','7777','--install-port','17778','--timeout','900','--agent-apk',str(AGENT),'--agent-sha256',record['agent_apk_sha256']]
env={**os.environ,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','ASHNAZG_ADB':'/host-adb-disabled','ASHNAZG_CI_DIAGNOSTICS':'1'}
try:
 if MODE=='cold':
  assert not STATE.exists(),'cold run requires fresh state'
  subprocess.run(args+['--validate-only'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,env=env,timeout=90,check=True)
 with (RUN/(MODE+'-vm.stdout')).open('w') as output,(RUN/(MODE+'-vm.stderr')).open('w') as errors:
  process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=output,stderr=errors,env=env,start_new_session=True)
  record['helper_pid']=process.pid;save();start=time.monotonic();progress=start
  while time.monotonic()-start<965:
   if process.poll() is not None:raise RuntimeError('helper exited before agent readiness')
   ready=None
   for raw in (RUN/(MODE+'-vm.stdout')).read_text().splitlines():
    try:value=json.loads(raw)
    except ValueError:continue
    if value.get('status')=='agent_ready':ready=value;break
   if ready:break
   if time.monotonic()>=progress:
    print('Native session starting: elapsed=%ds'%int(time.monotonic()-start),flush=True);progress=time.monotonic()+15
   time.sleep(.25)
  else:raise RuntimeError('agent readiness deadline exceeded')
  record['ready_receipt']=ready;record['observed_ready_seconds']=round(time.monotonic()-start,3)
  credential=STATE/'.ashnazg-session-v1.json'
  assert stat.S_IMODE(credential.stat().st_mode)==0o600 and stat.S_IMODE(STATE.stat().st_mode)==0o700
  credentials=json.loads(credential.read_text());secrets=[credentials['rpc_token'],credentials['install_token']]
  assert credentials['helper_pid']==process.pid and credentials['manifest_source_sha']==record['source_sha']
  record['session_checks'].append('private_matching_credentials')
  print('Native agent ready; verifying install and direct RPC paths',flush=True);save()
  install(credentials,FIXTURE,bad_token=True);record['session_checks'].append('invalid_install_auth_rejected_before_payload')
  install(credentials,FIXTURE,bad_hash=True);record['session_checks'].append('corrupt_upload_rejected')
  record.setdefault('installs',[]).append(install(credentials,FIXTURE,'dev.ashnazg.fixture'))
  record['session_checks'].append('valid_install_and_launch_after_rejections')
  client=checks.RpcClient(credentials['rpc_token'],timeout=30)
  checks.wait_for_framework_ready(client,'dev.ashnazg.fixture:id/increment','Count: 0')
  record['rpc_checks']=checks.verify_rpc(client,RUN/(MODE+'-rpc'))
  record['rpc_checks']+=framework(client,credentials,'compose','com.ashnazg.fixture.compose','compose-button','Compose count: 0','Compose count: 1')
  record['rpc_checks']+=framework(client,credentials,'reactnative','com.ashnazg.fixture.reactnative','rn-button','React Native count: 0','React Native count: 1')
  record['mcp_checks']=mcp(credentials)
  record['mcp_checks']+=checks.verify_http_auth(str(HOST),SimpleNamespace(token=credentials['rpc_token'],serial='native-session-no-host-adb'))
  record['status']='tests_passed';save()
except BaseException as error:
 failed=True;record['status']='failed';record['error_type']=type(error).__name__
 # Error details can contain fixture data; secrets are always redacted.
 detail=str(error)[:2000]
 for secret in secrets:detail=detail.replace(secret,'<redacted>')
 record['error']=detail;save();print('Native acceptance failed: '+record['error_type'],flush=True)
finally:
 if client:client.close()
 if process and process.poll() is None:
  process.terminate()
  try:process.wait(timeout=105)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10);failed=True;record['forced_shutdown']=True
 if process:record['helper_exit_code']=process.returncode
 runtime=STATE/'runtime.json'
 if runtime.exists():record['final_runtime_receipt']=json.loads(runtime.read_text())
 record['credential_removed']=not (STATE/'.ashnazg-session-v1.json').exists()
 closed={}
 for port in (15555,7777,17778):
  with socket.socket() as probe:probe.settimeout(.5);closed[str(port)]=probe.connect_ex(('127.0.0.1',port))!=0
 record['owned_listeners_closed']=closed
 if not failed and (record.get('helper_exit_code')!=0 or record.get('final_runtime_receipt',{}).get('status')!='stopped' or not record['credential_removed'] or not all(closed.values())):failed=True
 for path in (RUN/(MODE+'-vm.stdout'),RUN/(MODE+'-vm.stderr'),RUN/(MODE+'-mcp.stderr')):
  if path.exists() and any(s.encode() in path.read_bytes() for s in secrets):failed=True;record['secret_in_output']=True
 record['status']='failed' if failed else 'passed';save()
 print(json.dumps({'status':record['status'],'mode':MODE,'rpc_checks':len(record['rpc_checks']),'mcp_checks':len(record['mcp_checks']),'run':str(RUN)}),flush=True)
sys.exit(1 if failed else 0)
