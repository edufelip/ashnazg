use std::{io::Write,path::PathBuf,time::{Duration,Instant}};
use anyhow::{Result,ensure,anyhow};
use serde::Deserialize;
use tokio::io::{AsyncBufReadExt,BufReader};
use tokio_util::sync::CancellationToken;
use ashnazg::native_session::{NativeSession,NativeSessionLaunch};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config { helper:PathBuf,helper_sha256:String,assets:PathBuf,state_dir:PathBuf,agent_apk:PathBuf,agent_apk_sha256:String,source_sha:String,adb_port:u16,agent_port:u16,install_port:u16,startup_timeout_secs:u16 }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {op:String,path:Option<PathBuf>,package:Option<String>}
fn emit(value:serde_json::Value)->Result<()> { let mut out=std::io::stdout().lock();writeln!(out,"{value}")?;out.flush()?;Ok(()) }
async fn interact(session:&NativeSession,cancel:&CancellationToken)->Result<()> {
 let guest=session.guest_client()?;
 let pong=guest.call("ping",serde_json::json!({})).await?;
 ensure!(pong["apiLevel"].as_u64().is_some_and(|n|n>=34),"proof ping failed");
 let installer=session.install_client()?;
 emit(serde_json::json!({"phase":"ready","helper_pid":session.helper_pid(),"session_id":session.session_id(),"api_level":pong["apiLevel"]}))?;
 let mut input=BufReader::new(tokio::io::stdin());

 loop {
  let mut line=String::new();
  let count=tokio::select! {
   n=input.read_line(&mut line)=>n?,
   _=session.wait_terminated()=>return Err(anyhow!("owned helper terminated unexpectedly")),
   _=cancel.cancelled()=>break,
  };
  if count==0 {break;}
  ensure!(count<=4096,"proof request too large");
  let request:Request=serde_json::from_str(&line).map_err(|_|anyhow!("invalid proof request"))?;
  match request.op.as_str() {
   "shutdown"=>break,
   "install"=>{
    let result=installer.install(request.path.ok_or_else(||anyhow!("missing proof APK"))?,request.package,Instant::now()+Duration::from_secs(180),cancel.child_token()).await?;
    emit(serde_json::json!({"phase":"installed","launched":result.launched,"package":result.package}))?;
   },
   _=>return Err(anyhow!("invalid proof operation")),
  }
 }
 Ok(())
}
#[tokio::main]
async fn main()->Result<()> {
 let path=std::env::args_os().nth(1).ok_or_else(||anyhow!("missing proof configuration"))?;
 let c:Config=serde_json::from_slice(&std::fs::read(path)?)?;
 let timeout=c.startup_timeout_secs;
 let launch=NativeSessionLaunch{helper:c.helper,helper_sha256:c.helper_sha256,assets:c.assets,state_dir:c.state_dir,agent_apk:c.agent_apk,agent_apk_sha256:c.agent_apk_sha256,source_sha:c.source_sha,adb_port:c.adb_port,agent_port:c.agent_port,install_port:c.install_port,startup_timeout_secs:timeout};
 let cancel=CancellationToken::new();
 let signals=cancel.clone();
 let mut term=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
 let signal_task=tokio::spawn(async move {tokio::select!{_=term.recv()=>{},_=tokio::signal::ctrl_c()=>{}};signals.cancel();});
 let session=NativeSession::start(launch,Instant::now()+Duration::from_secs(u64::from(timeout)),cancel.clone()).await?;
 let result=interact(&session,&cancel).await;
 let stopped=session.shutdown().await;
 if let Ok(exit)=&stopped {emit(serde_json::json!({"phase":"stopped","code":exit.code,"signal":exit.signal,"forced":exit.forced,"credential_absent":exit.credential_absent,"parent_cancelled":cancel.is_cancelled()}))?;}
 signal_task.abort();let _=signal_task.await;
 result?;stopped?;Ok(())
}
