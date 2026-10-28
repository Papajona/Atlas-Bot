
const $=id=>document.getElementById(id);
sessionStorage.removeItem('ait_admin_refresh_token');sessionStorage.removeItem('ait_approver_token');
function esc(v){return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;')}
function token(){return sessionStorage.getItem('ait_admin_access_token')||''}
function jwtClaims(){const part=token().split('.')[1];if(!part)return {};try{const b=part.replace(/-/g,'+').replace(/_/g,'/');return JSON.parse(atob(b.padEnd(Math.ceil(b.length/4)*4,'=')))}catch{return {}}}
function actorId(){const c=jwtClaims();return String(c.sub||c.user_id||'').trim()}
function syncActorIdentity(){const id=actorId();$('adminId').value=id;$('releaseId').value=id;$('operatorIdentity').textContent=id?'Authenticated identity: '+id+'. Sign out and sign in as a different authorized treasury user before releasing a withdrawal you approved.':'Unable to derive the user identity from this access token. Sign in again.';$('adminLogoutBtn').hidden=!id;$('adminEnrollBtn').hidden=loggedIn()||!!window.adminMfaFactorId}
const adminTotpFlag=document.body.dataset.adminTotpRequired;
const adminMfaRequired=adminTotpFlag!=='false'; // Fail closed when a template is rendered without server-side settings.
let dashboardReady=false;
let adminSessionEpoch=0;
let adminMfaVerificationInFlight=false;
let dashboardRefreshTimer=null;
let withdrawalRefreshTimer=null;
function loggedIn(){return dashboardReady&&!!token()&&(!adminMfaRequired||sessionStorage.getItem('ait_admin_mfa_verified')==='true')}
function headers(){const h={'Content-Type':'application/json'};if(token())h['Authorization']='Bearer '+token();const legacy=sessionStorage.getItem('ait_admin_token')||'';if(!token()&&legacy)h['X-Admin-Token']=legacy;return h}
function detailText(d){if(Array.isArray(d))return d.map(e=>e&&e.msg?(((e.loc||[]).slice(1).join('.')+': ')+e.msg).replace(/^: /,''):String(e)).join('; ');if(d&&typeof d==='object')return JSON.stringify(d);return d?String(d):''}
async function api(path,opts={}){
  if(!dashboardReady&&!String(path).startsWith('/api/admin/auth/')) {
    throw new Error('Complete administrator sign-in and server-verified MFA before accessing protected dashboard data.');
  }
  const requestEpoch=adminSessionEpoch;
  const requestToken=token();
  const r=await fetch(path,{...opts,headers:{...headers(),...(opts.headers||{})}});
  const text=await r.text();let j={};try{j=JSON.parse(text)}catch{}
  const sameSession=requestEpoch===adminSessionEpoch&&requestToken===token();
  if(r.status===401){
    if(sameSession)lockAdminDashboard('Administrator session expired or was rejected. Sign in again.');
    throw new Error(detailText(j.detail)||text||'Administrator session expired.');
  }
  if(!sameSession)throw new Error('Discarded stale administrator response after the session changed.');
  if(!r.ok)throw new Error(detailText(j.detail)||text||'Request failed');
  return j;
}
function lockAdminDashboard(message,clearTokens=true){
  adminSessionEpoch+=1;
  adminMfaVerificationInFlight=false;
  dashboardReady=false;
  document.body.classList.remove('admin-session-ready');
  sessionStorage.removeItem('ait_admin_mfa_verified');
  if(clearTokens){
    sessionStorage.removeItem('ait_admin_access_token');
    sessionStorage.removeItem('ait_admin_refresh_token');
    sessionStorage.removeItem('ait_admin_token');
    sessionStorage.removeItem('ait_approver_token');
  }
  window.adminMfaFactorId=null;
  window.adminMfaChallengeId=null;
  if(dashboardRefreshTimer!==null)clearInterval(dashboardRefreshTimer);
  if(withdrawalRefreshTimer!==null)clearInterval(withdrawalRefreshTimer);
  dashboardRefreshTimer=null;
  withdrawalRefreshTimer=null;
  $('mode').textContent='SIGN IN REQUIRED';
  $('adminId').value='';
  $('releaseId').value='';
  $('withdrawalRows').replaceChildren();
  $('trades').replaceChildren();
  $('roles').replaceChildren();
  $('adminMfaBox').style.display='none';
  $('adminMfaVerifyBtn').style.display='none';
  $('adminMfaEnrollment').hidden=true;
  $('adminMfaQr').removeAttribute('src');
  $('adminMfaSecret').textContent='';
  $('adminMfaCode').value='';
  if(message)$('authState').textContent=message;
  $('adminLogoutBtn').hidden=!token();
  $('adminEnrollBtn').hidden=!token();
}
function activateDashboard(){
  if(!token()||(adminMfaRequired&&sessionStorage.getItem('ait_admin_mfa_verified')!=='true')){
    lockAdminDashboard('The server has not confirmed the required administrator authentication level. Protected data remains locked.');
    return;
  }
  dashboardReady=true;
  document.body.classList.add('admin-session-ready');
  $('mode').textContent='VERIFIED ADMIN';
  $('authState').textContent='Administrator session verified by the server. Protected dashboard data is loading.';
  syncActorIdentity();
  refresh();loadWithdrawals();loadRoles();loadMargin();
  if(dashboardRefreshTimer===null)dashboardRefreshTimer=setInterval(()=>{if(dashboardReady)refresh()},15000);
  if(withdrawalRefreshTimer===null)withdrawalRefreshTimer=setInterval(()=>{if(dashboardReady)loadWithdrawals()},20000);
}
async function beginAdminMfaChallenge(status,expectedEpoch=adminSessionEpoch){
  if(expectedEpoch!==adminSessionEpoch)return;
  if(!status?.enabled||!Array.isArray(status.factor_ids)||!status.factor_ids.length){
    $('authState').textContent='No verified authenticator is available. Enroll Google Authenticator to continue.';
    $('adminEnrollBtn').hidden=false;
    return;
  }
  const factorId=String(status.factor_ids[0]);
  const challenge=await api('/api/admin/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:factorId})});
  if(expectedEpoch!==adminSessionEpoch)return;
  if(!challenge?.id)throw new Error('The authenticator challenge did not return an ID.');
  window.adminMfaFactorId=factorId;
  window.adminMfaChallengeId=String(challenge.id);
  $('adminMfaBox').style.display='block';
  $('adminMfaVerifyBtn').style.display='inline-block';
  $('adminEnrollBtn').hidden=true;
  $('adminLogoutBtn').hidden=false;
  $('authState').textContent='Enter the six-digit Google Authenticator code. Protected data remains locked until the server confirms AAL2.';
  $('adminMfaCode').focus();
}
async function resumeAdminSession(){
  const attemptEpoch=adminSessionEpoch;
  if(!token()){
    lockAdminDashboard('Sign in with an authorized administrator account and complete required Google Authenticator MFA.');
    return;
  }
  $('adminLogoutBtn').hidden=false;
  if(!adminMfaRequired){
    sessionStorage.setItem('ait_admin_mfa_verified','true');
    activateDashboard();
    return;
  }
  try{
    const status=await api('/api/admin/auth/mfa/status');
    if(attemptEpoch!==adminSessionEpoch)return;
    if(status.enabled&&status.aal==='aal2'){
      sessionStorage.setItem('ait_admin_mfa_verified','true');
      activateDashboard();
    }else if(status.enabled){
      sessionStorage.removeItem('ait_admin_mfa_verified');
      await beginAdminMfaChallenge(status,attemptEpoch);
    }else{
      sessionStorage.removeItem('ait_admin_mfa_verified');
      $('adminEnrollBtn').hidden=false;
      $('authState').textContent='Signed in, but no verified authenticator exists. Enroll Google Authenticator; all protected data remains locked.';
    }
  }catch(error){
    if(attemptEpoch===adminSessionEpoch)lockAdminDashboard('The administrator session could not be verified. Sign in again. '+error.message);
  }
}
async function adminLogin(){
  lockAdminDashboard('Signing in…',true);
  const attemptEpoch=adminSessionEpoch;
  try{
    const response=await fetch('/api/admin/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('adminEmail').value.trim(),password:$('adminPassword').value})});
    if(attemptEpoch!==adminSessionEpoch)return;
    const result=await response.json().catch(()=>({}));
    if(attemptEpoch!==adminSessionEpoch)return;
    if(!response.ok)throw new Error(detailText(result.detail)||detailText(result.message)||'Administrator login failed');
    if(!result.access_token)throw new Error('Authentication response did not contain an access token.');
    sessionStorage.setItem('ait_admin_access_token',result.access_token);
    await resumeAdminSession();
  }catch(error){
    if(attemptEpoch===adminSessionEpoch)lockAdminDashboard('Admin sign-in: '+error.message);
  }
}
async function adminEnroll(){
  if(!token()){$('authState').textContent='Sign in first.';return}
  if(dashboardReady){$('authState').textContent='This administrator session is already verified.';return}
  const attemptEpoch=adminSessionEpoch;
  try{
    const enrollment=await api('/api/admin/auth/mfa/enroll',{method:'POST',body:'{}'});
    if(attemptEpoch!==adminSessionEpoch)return;
    if(!enrollment?.id||!enrollment?.totp?.qr_code)throw new Error('The authenticator service did not return an enrollment ID and QR code.');
    window.adminMfaFactorId=String(enrollment.id);
    $('adminMfaQr').src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(enrollment.totp.qr_code);
    $('adminMfaSecret').textContent=enrollment.totp.secret||'No manual key provided';
    $('adminMfaEnrollment').hidden=false;
    $('adminMfaBox').style.display='block';
    $('adminLogoutBtn').hidden=false;
    $('authState').textContent='Authenticator enrollment was created. Scan the QR and enter its six-digit code; protected data remains locked.';
    const challenge=await api('/api/admin/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:window.adminMfaFactorId})});
    if(attemptEpoch!==adminSessionEpoch)return;
    if(!challenge?.id)throw new Error('The enrollment challenge did not return an ID.');
    window.adminMfaChallengeId=String(challenge.id);
    $('adminMfaVerifyBtn').style.display='inline-block';
    $('adminEnrollBtn').hidden=true;
    $('authState').textContent='Scan the enrollment QR with Google Authenticator and enter its six-digit code. Protected data remains locked.';
    $('adminMfaCode').focus();
  }catch(error){
    if(attemptEpoch!==adminSessionEpoch)return;
    $('authState').textContent='Authenticator enrollment: '+error.message;
    if(window.adminMfaFactorId&&token()){
      $('adminMfaBox').style.display='block';
      $('adminMfaVerifyBtn').style.display='inline-block';
      $('adminEnrollBtn').hidden=true;
      $('adminLogoutBtn').hidden=false;
      $('authState').textContent+=' You may retry verification; a fresh challenge will be requested if needed.';
    }
  }
}
async function adminMfaVerify(){
  if(adminMfaVerificationInFlight||dashboardReady)return;
  const attemptEpoch=adminSessionEpoch;
  const verificationToken=token();
  const code=$('adminMfaCode').value.trim();
  if(!/^[0-9]{6}$/.test(code)){$('authState').textContent='Enter the six-digit Google Authenticator code.';$('adminMfaCode').focus();return}
  if(!token()||!window.adminMfaFactorId){$('authState').textContent='Sign in and start authenticator enrollment before verifying.';return}
  adminMfaVerificationInFlight=true;
  let tokenUpgraded=false;
  try{
    if(!window.adminMfaChallengeId){
      const challenge=await api('/api/admin/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:window.adminMfaFactorId})});
      if(attemptEpoch!==adminSessionEpoch)return;
      if(!challenge?.id)throw new Error('The authenticator challenge did not return an ID.');
      window.adminMfaChallengeId=String(challenge.id);
    }
    const verified=await api('/api/admin/auth/mfa/verify',{method:'POST',body:JSON.stringify({factor_id:window.adminMfaFactorId,challenge_id:window.adminMfaChallengeId,code})});
    if(attemptEpoch!==adminSessionEpoch)return;
    if(!verified.access_token)throw new Error('Authenticator verification did not return an upgraded session. Sign in again.');
    sessionStorage.setItem('ait_admin_access_token',verified.access_token);
    tokenUpgraded=true;
    window.adminMfaChallengeId=null;
    $('authState').textContent='Authenticator code accepted. Checking the server-confirmed AAL2 session…';
    const status=await api('/api/admin/auth/mfa/status');
    if(attemptEpoch!==adminSessionEpoch)return;
    if(!status.enabled||status.aal!=='aal2'){
      lockAdminDashboard('Authenticator response received, but the server did not confirm AAL2. Protected data remains locked.');
      return;
    }
    sessionStorage.setItem('ait_admin_mfa_verified','true');
    window.adminMfaChallengeId=null;
    $('adminMfaCode').value='';
    $('adminMfaBox').style.display='none';
    $('adminMfaVerifyBtn').style.display='none';
    $('adminMfaEnrollment').hidden=true;
    $('adminMfaQr').removeAttribute('src');
    $('adminMfaSecret').textContent='';
    activateDashboard();
  }catch(error){
    if(attemptEpoch!==adminSessionEpoch||dashboardReady||(!tokenUpgraded&&verificationToken!==token()))return;
    window.adminMfaChallengeId=null;
    if(window.adminMfaFactorId&&token()){
      $('adminMfaBox').style.display='block';
      $('adminMfaVerifyBtn').style.display='inline-block';
      $('adminLogoutBtn').hidden=false;
    }
    $('authState').textContent='Authenticator verification: '+error.message+' You can retry; a fresh challenge will be requested.';
  }finally{
    if(attemptEpoch===adminSessionEpoch)adminMfaVerificationInFlight=false;
  }
}
function adminLogout(){lockAdminDashboard('Signed out.',true);$('approverToken').value='';$('releaseToken').value='';$('releaseSignature').value='';$('adminMfaCode').value='';$('adminMfaEnrollment').hidden=true;$('adminMfaQr').removeAttribute('src');$('adminMfaSecret').textContent='';$('operatorIdentity').textContent='Signed out. Sign in as a different authorized user to change operator identity.';}
document.addEventListener('click',event=>{const el=event.target.closest('[data-action]');if(!el)return;const id=Number(el.dataset.withdrawalId);const actions={'toggle-nav':()=>document.body.classList.toggle('nav-open'),'admin-login':adminLogin,'admin-enroll':adminEnroll,'admin-mfa-verify':adminMfaVerify,'admin-logout':adminLogout,'train':train,'backtest':backtest,'strategy-backtest':strategyBacktest,'backtest-all':backtestAll,'meta-label-audit':metaLabelAudit,'ai-review':aiReview,'paper-signals':paperSignals,'signal':signal,'strategy-signal':strategySignal,'paper-step':paperStep,'halt':halt,'reset-risk':resetRisk,'disable-live':disableLive,'load-margin':loadMargin,'load-withdrawals':loadWithdrawals,'load-roles':loadRoles,'save-role':saveRole,'reconcile':reconcile,'approve-withdrawal':()=>approveWithdrawal(id),'reject-withdrawal':()=>rejectWithdrawal(id),'execute-withdrawal':()=>executeWithdrawal(id),'prepare-proposal':()=>prepareWithdrawalProposal(id),'reconcile-withdrawal':()=>reconcileWithdrawal(id)};const fn=actions[el.dataset.action];if(fn){event.preventDefault();fn()}});
document.addEventListener('change',event=>{if(event.target.dataset.actionChange==='load-withdrawals')loadWithdrawals()});

