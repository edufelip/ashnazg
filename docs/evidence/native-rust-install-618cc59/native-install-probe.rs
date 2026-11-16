use std::{env,fs::OpenOptions,io::Read,os::unix::fs::{MetadataExt,OpenOptionsExt},path::PathBuf,time::{Duration,Instant}};
use anyhow::{Result,ensure,anyhow};
use serde::Deserialize;
use zeroize::{Zeroizing,Zeroize};
use ashnazg::native_install::NativeInstallClient;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Credentials {schema_version:u64,session_id:String,helper_pid:u32,manifest_source_sha:String,agent_apk_sha256:String,rpc_token:String,install_token:String,agent_listen:String,install_listen:String}
impl Drop for Credentials {fn drop(&mut self){self.rpc_token.zeroize();self.install_token.zeroize();}}
#[tokio::main]
async fn main()->Result<()> {
 let args:Vec<_>=env::args().collect();ensure!(args.len()==5||args.len()==6,"invalid proof arguments");
 let file=OpenOptions::new().read(true).custom_flags(libc_flags()).open(&args[1]).map_err(|_|anyhow!("cannot read owned test credentials"))?;
 let m=file.metadata()?;ensure!(m.is_file()&&m.uid()==libc_uid()&&m.mode()&0o7777==0o600&&m.len()<=4096,"invalid owned test credentials");
 let mut raw=Zeroizing::new(Vec::new());file.take(4097).read_to_end(&mut raw)?;ensure!(raw.len()<=4096,"oversized test credentials");
 let credentials:Credentials=serde_json::from_slice(&raw).map_err(|_|anyhow!("invalid test credentials"))?;
 ensure!(credentials.schema_version==1&&credentials.helper_pid==args[2].parse::<u32>()?&&credentials.manifest_source_sha==args[3],"test session mismatch");
 ensure!(credentials.agent_apk_sha256.len()==64&&credentials.agent_listen.starts_with("127.0.0.1:"),"invalid test session");
 let client=NativeInstallClient::new(credentials.install_listen.parse()?,credentials.session_id.clone(),credentials.install_token.clone())?;
 let package=args.get(5).cloned();
 let result=client.install(PathBuf::from(&args[4]),package,Instant::now()+Duration::from_secs(180),tokio_util::sync::CancellationToken::new()).await?;
 println!("{}",serde_json::json!({"status":"installed","launched":result.launched,"package":result.package}));Ok(())
}
// Test-only provenance reader; the production session owner is separate work.
fn libc_flags()->i32 {libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK}
fn libc_uid()->u32 {unsafe{libc::getuid()}}
