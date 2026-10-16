
const $=id=>document.getElementById(id);
sessionStorage.removeItem('ait_admin_refresh_token');sessionStorage.removeItem('ait_approver_token');
function esc(v){return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;')}
function token(){return sessionStorage.getItem('ait_admin_access_token')||''}
function jwtClaims(){const part=token().split('.')[1];if(!part)return {};try{const b=part.replace(/-/g,'+').replace(/_/g,'/');return JSON.parse(atob(b.padEnd(Math.ceil(b.length/4)*4,'=')))}catch{return {}}}
function actorId(){const c=jwtClaims();return String(c.sub||c.user_id||'').trim()}
function syncActorIdentity(){const id=actorId();$('adminId').value=id;$('releaseId').value=id;$('operatorIdentity').textContent=id?'Authenticated identity: '+id+'. Sign out and sign in as a different authorized treasury user before releasing a withdrawal you approved.':'Unable to derive the user identity from this access token. Sign in again.';$('adminLogoutBtn').hidden=!id}
function loggedIn(){return sessionStorage.getItem('ait_admin_mfa_verified')==='true'&&!!(token()||sessionStorage.getItem('ait_admin_token'))}
function headers(){const h={'Content-Type':'application/json'};if(token())h['Authorization']='Bearer '+token();const legacy=sessionStorage.getItem('ait_admin_token')||'';if(!token()&&legacy)h['X-Admin-Token']=legacy;return h}
function detailText(d){if(Array.isArray(d))return d.map(e=>e&&e.msg?(((e.loc||[]).slice(1).join('.')+': ')+e.msg).replace(/^: /,''):String(e)).join('; ');if(d&&typeof d==='object')return JSON.stringify(d);return d?String(d):''}
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{...headers(),...(opts.headers||{})}});const text=await r.text();let j={};try{j=JSON.parse(text)}catch{}if(!r.ok)throw new Error(detailText(j.detail)||text||'Request failed');return j}
async function adminLogin(){
  sessionStorage.removeItem('ait_admin_mfa_verified');sessionStorage.removeItem('ait_admin_access_token');sessionStorage.removeItem('ait_admin_token');
  try{
    const r=await fetch('/api/admin/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('adminEmail').value.trim(),password:$('adminPassword').value})});
    const j=await r.json(); if(!r.ok) throw new Error(detailText(j.detail)||detailText(j.message)||'Administrator login failed');
    sessionStorage.setItem('ait_admin_access_token',j.access_token);syncActorIdentity();

    if(j.totp_enrolled&&j.mfa_required){
      const st=await api('/api/admin/auth/mfa/status'); window.adminMfaFactorId=st.factor_ids?.[0]||null;
      if(!window.adminMfaFactorId)throw new Error('No verified Google Authenticator factor is available.');
      const ch=await api('/api/admin/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:window.adminMfaFactorId})});
      window.adminMfaChallengeId=ch.id; $('adminMfaBox').style.display='block'; $('adminMfaVerifyBtn').style.display='inline-block'; $('authState').textContent='Enter the 6-digit Google Authenticator code.';
    }else if(j.mfa_required){sessionStorage.removeItem('ait_admin_access_token');syncActorIdentity();$('authState').textContent='Enroll Google Authenticator before administrator access is granted.';}else{sessionStorage.setItem('ait_admin_mfa_verified','true');syncActorIdentity();refresh();loadWithdrawals();loadRoles();loadMargin()}
  }catch(e){$('authState').textContent='Admin sign-in: '+e.message}
}
async function adminEnroll(){
  if(!token()){ $('authState').textContent='Sign in first.'; return; }
  // Open synchronously in the click gesture so browser popup blockers do not discard
  // the QR window after the network request completes.
  const w=window.open('','_blank','width=420,height=520');
  if(!w){$('authState').textContent='Allow popups for this site to view the Authenticator QR code.';return}
  w.document.write('<title>Atlas Admin Authenticator</title><p>Loading authenticator setup…</p>');
  try{
    const d=await api('/api/admin/auth/mfa/enroll',{method:'POST',body:'{}'});
    $('output').textContent='Scan this Google Authenticator QR code with your administrator device.\nManual secret: '+(d.totp?.secret||'unavailable');
    if(d.totp?.qr_code){
      w.document.open();
      w.document.write('<title>Atlas Admin Authenticator</title><p>Scan with Google Authenticator, then return to Atlas.</p><img style="max-width:360px" alt="Authenticator enrollment QR code" src="data:image/svg+xml;charset=utf-8,'+encodeURIComponent(d.totp.qr_code)+'"><p>Manual secret: '+esc(d.totp.secret||'unavailable')+'</p>');
      w.document.close();
    }else{w.close();throw new Error('Authenticator provider did not return a QR code.')}
  }catch(e){if(!w.closed)w.close();$('authState').textContent='Authenticator enrollment: '+e.message}
}
async function adminMfaVerify(){
  const code=$('adminMfaCode').value.trim();
  if(!/^[0-9]{6}$/.test(code)){ $('authState').textContent='Enter the 6-digit Google Authenticator code.'; return }
  try{
    const d=await api('/api/admin/auth/mfa/verify',{method:'POST',body:JSON.stringify({factor_id:window.adminMfaFactorId,challenge_id:window.adminMfaChallengeId,code})});
    if(d.access_token)sessionStorage.setItem('ait_admin_access_token',d.access_token);sessionStorage.setItem('ait_admin_mfa_verified','true');syncActorIdentity();
    window.adminMfaChallengeId=null; $('adminMfaBox').style.display='none'; $('adminMfaVerifyBtn').style.display='none'; $('adminMfaCode').value=''; $('authState').textContent='Administrator AAL2 session active.'; refresh(); loadWithdrawals(); loadRoles();
  }catch(e){window.adminMfaChallengeId=null;$('authState').textContent='Authenticator verification: '+e.message}
}
function req(){return {asset:$('asset').value,symbol:$('symbol').value.trim(),exchange:$('exchange').value.trim(),timeframe:$('tf').value,days:+$('days').value,folds:+$('folds').value,min_train:800}}
async function loadWithdrawals(){
  try{
    const [summary,rows]=await Promise.all([api('/api/admin/withdrawals/summary'),api('/api/admin/withdrawals?status='+encodeURIComponent($('wdFilter').value))]);
    $('wdPending').textContent=summary.pending_count;
    $('wdPendingAmount').textContent='$'+Number(summary.pending_amount||0).toLocaleString(undefined,{maximumFractionDigits:2});
    $('wdApproved').textContent=summary.approved_count;
    $('wdApprovedAmount').textContent=Number(summary.approved_amount||0).toLocaleString(undefined,{maximumFractionDigits:2})+' USDT reserved';
    $('wdInFlight').textContent=summary.in_flight_count;
    $('wdRejected').textContent=summary.rejected_count;
    $('wdError').textContent='';
    window.wdRows=Object.fromEntries(rows.map(r=>[r.id,r]));
    $('withdrawalRows').innerHTML=rows.map(x=>{
      const canApprove=x.status==='PENDING'||x.status==='PARTIALLY_APPROVED';
      const digestValid=/^[a-f0-9]{64}$/i.test(String(x.proposal_digest||''));
      const action=canApprove?`<button type="button" data-action="approve-withdrawal" data-withdrawal-id="${Number(x.id)}">Approve</button><button type="button" class="danger" data-action="reject-withdrawal" data-withdrawal-id="${Number(x.id)}">Reject</button>`:(x.status==='APPROVED'?(digestValid?`<button type="button" data-action="execute-withdrawal" data-withdrawal-id="${Number(x.id)}">Execute</button>`:`<button type="button" data-action="prepare-proposal" data-withdrawal-id="${Number(x.id)}">Prepare proposal digest</button>`):(x.status==='SUBMITTED'||x.status==='UNKNOWN'?`<button type="button" data-action="reconcile-withdrawal" data-withdrawal-id="${Number(x.id)}">Reconcile</button>`:'—'));
      return `<tr><td>${esc(x.request_id)}</td><td><b>${esc(x.customer_email||'Customer '+(x.customer_id||x.account_ref))}</b><small class="muted"> #${esc(x.customer_id||x.account_ref)}</small></td><td>${Number(x.amount).toLocaleString(undefined,{maximumFractionDigits:6})} ${esc(x.currency)}</td><td>${esc(x.destination_masked)}</td><td>${x.risk_score==null?'n/a':Number(x.risk_score).toFixed(2)} ${x.risk_flags?.length?'<span class="tag">FLAGGED</span>':''}</td><td>${Number(x.approval_count)}/${Number(x.required_approvals)}</td><td><span class="tag">${esc(x.status)}</span>${x.status==='APPROVED' ? '<small class="muted">Proposal SHA-256: <code>'+esc(x.proposal_digest||'missing — prepare before execute')+'</code></small>' : ''}</td><td>${action}</td></tr>`;
    }).join('');
  }catch(e){$('wdError').textContent='Withdrawal queue: '+e.message}
}
async function approveWithdrawal(id){
  const admin=$('adminId').value.trim(); const approver=$('approverToken').value||''; if(!loggedIn()||!admin){$('output').textContent='Sign in and complete administrator MFA first.';return}
  try{const reason=prompt('Approval note (optional):','');const j=await api('/api/admin/withdrawals/'+id+'/approve',{method:'POST',headers:{'X-Approver-Token':approver},body:JSON.stringify({admin_id:admin,reason:reason||''})});$('output').textContent=JSON.stringify(j,null,2);loadWithdrawals()}catch(e){$('output').textContent='Approval failed: '+e.message}
}
async function executeWithdrawal(id){
  const operator=$('releaseId').value.trim(); const release=$('releaseToken').value||''; const signature=$('releaseSignature').value.trim()||null;
  if(!loggedIn()||!operator||!release){$('output').textContent='Sign in with the release operator account and provide its release token.';return}
  const wr=(window.wdRows||{})[id]||{};
  if(!/^[a-f0-9]{64}$/i.test(String(wr.proposal_digest||''))){$('output').textContent='A valid SHA-256 proposal digest is required. Prepare the proposal, then refresh the queue.';return}
  if(!confirm('Release payout #'+id+'\nAmount: '+(wr.amount??'?')+' '+(wr.currency||'')+'\nDestination: '+(wr.destination_masked||'?')+'\nProposal SHA-256: '+wr.proposal_digest+'\n\nThis sends funds on-chain and cannot be undone.'))return;
  try{const j=await api('/api/admin/withdrawals/'+id+'/release',{method:'POST',headers:{'X-Release-Token':release},body:JSON.stringify({operator_id:operator,signature})});$('output').textContent=JSON.stringify(j,null,2);loadWithdrawals()}catch(e){$('output').textContent='Execution failed: '+e.message}
}
async function prepareWithdrawalProposal(id){try{const d=await api('/api/admin/withdrawals/'+id+'/proposal',{method:'POST',body:'{}'});$('output').textContent='Proposal SHA-256: '+d.digest+'\nRequest: '+d.request_id+'\nAmount: '+d.payload.amount+' '+d.payload.currency+'\nNetwork: '+d.payload.network+'\nProvider: '+d.payload.provider+'\nRefresh the queue to execute only after checking the proposal.';await loadWithdrawals()}catch(e){$('output').textContent='Proposal preparation failed: '+e.message}}
async function reconcileWithdrawal(id){
  const operator=$('releaseId').value.trim(); const release=$('releaseToken').value||'';
  if(!operator||!release){$('output').textContent='Release operator ID and token are required.';return}
  try{const j=await api('/api/admin/withdrawals/'+id+'/reconcile',{method:'POST',headers:{'X-Release-Token':release},body:JSON.stringify({operator_id:operator})});$('output').textContent=JSON.stringify(j,null,2);loadWithdrawals()}catch(e){$('output').textContent='Reconciliation failed: '+e.message}
}
async function rejectWithdrawal(id){
  const admin=$('adminId').value.trim(); const approver=$('approverToken').value||''; if(!loggedIn()||!admin){$('output').textContent='Sign in and complete administrator MFA first.';return}
  const reason=prompt('Required rejection reason:',''); if(!reason?.trim())return;
  try{const j=await api('/api/admin/withdrawals/'+id+'/reject',{method:'POST',headers:{'X-Approver-Token':approver},body:JSON.stringify({admin_id:admin,reason})});$('output').textContent=JSON.stringify(j,null,2);loadWithdrawals()}catch(e){$('output').textContent='Rejection failed: '+e.message}
}

async function loadRoles(){try{const rows=await api('/api/admin/security/roles');$('roles').innerHTML=rows.map(r=>`<tr><td>${esc(r.auth_user_id)}</td><td>${esc(r.role)}</td><td>${r.active?'YES':'NO'}</td><td>${esc(r.created_at||'')}</td></tr>`).join('')||'<tr><td colspan="4">No database role assignments. Explicitly allowlisted admins bootstrap as ADMINISTRATOR until assigned.</td></tr>'}catch(e){$('roles').innerHTML='<tr><td colspan="4">Role management requires ADMINISTRATOR: '+esc(e.message)+'</td></tr>'}}
async function saveRole(){const uid=$('roleUserId').value.trim();if(!uid){$('output').textContent='Enter a Supabase Auth user UUID.';return}try{const j=await api('/api/admin/security/roles',{method:'POST',body:JSON.stringify({auth_user_id:uid,role:$('roleName').value,active:$('roleActive').value==='true'})});$('output').textContent=JSON.stringify(j,null,2);loadRoles()}catch(e){$('output').textContent='Role update failed: '+e.message}}
async function loadMargin(){try{const m=await api('/api/admin/billing/margin?months=1');$('marginRevenue').textContent='$'+Number(m.gross_subscription_revenue||0).toLocaleString(undefined,{maximumFractionDigits:2});$('marginCosts').textContent='$'+Number(m.operating_costs||0).toLocaleString(undefined,{maximumFractionDigits:2});$('marginRef').textContent='$'+Number(m.referral_commissions||0).toLocaleString(undefined,{maximumFractionDigits:2});$('marginPct').textContent=Number(m.contribution_margin_pct||0).toFixed(1)+'%';$('marginProfit').textContent='Contribution $'+Number(m.contribution_profit||0).toLocaleString(undefined,{maximumFractionDigits:2});}catch(e){$('output').textContent='Margin: '+e.message}}
async function refresh(){try{const s=await api('/api/state');$('mode').textContent=s.mode;$('equity').textContent='$'+Number(s.equity).toLocaleString(undefined,{maximumFractionDigits:2});$('pnl').textContent=`Realized ${s.realized_pnl.toFixed(2)} · Unrealized ${s.unrealized_pnl.toFixed(2)}`;$('dd').textContent=(s.drawdown*100).toFixed(2)+'%';$('daily').textContent='Daily loss '+(s.daily_loss*100).toFixed(2)+'%';$('engine').textContent=s.live_enabled?'LIVE':'PAPER';$('live').textContent='Live switch '+(s.live_enabled?'ON':'OFF');$('kill').textContent=s.kill_switch?'HALTED':'ARMED';$('positions').textContent='Positions '+s.open_positions.length;let t=await api('/api/trades');$('trades').innerHTML=t.map(x=>`<tr><td>${esc(new Date(x.created_at).toLocaleString())}</td><td>${esc(x.symbol)}</td><td>${esc(x.side)}</td><td>${Number(x.requested_quantity).toFixed(6)}</td><td>${Number(x.filled_quantity).toFixed(6)}</td><td>${Number(x.average_fill_price||x.requested_price).toFixed(6)}</td><td>${esc(x.mode)}</td><td><span class="tag">${esc(x.status)}</span></td></tr>`).join('')}catch(e){$('output').textContent=e.message}}
async function run(path,body=req()){ $('output').textContent='Working…';try{const j=await api(path,{method:'POST',body:JSON.stringify(body)});$('output').textContent=JSON.stringify(j,null,2);refresh()}catch(e){$('output').textContent='ERROR: '+e.message}}
function train(){run('/api/train')} function backtest(){run('/api/backtest')} function strategyBacktest(){run('/api/strategy/backtest')} function backtestAll(){run('/api/research/backtest-all')} function aiReview(){run('/api/research/review')} function paperSignals(){run('/api/research/paper-signals')} function signal(){run('/api/signal')} function strategySignal(){run('/api/strategy/signal')} function paperStep(){run('/api/paper/step')}
async function reconcile(){run('/api/reconcile',req())}
async function halt(){try{$('output').textContent=JSON.stringify(await api('/api/risk/kill',{method:'POST'}),null,2);refresh()}catch(e){$('output').textContent=e.message}}
async function resetRisk(){try{$('output').textContent=JSON.stringify(await api('/api/risk/reset',{method:'POST'}),null,2);refresh()}catch(e){$('output').textContent=e.message}}
async function disableLive(){try{$('output').textContent=JSON.stringify(await api('/api/live/disable',{method:'POST'}),null,2);refresh()}catch(e){$('output').textContent=e.message}}
if(loggedIn()){syncActorIdentity();$('authState').textContent='Administrator MFA-verified session active.';refresh();loadWithdrawals();loadRoles();loadMargin()}else{$('mode').textContent='SIGN IN REQUIRED';$('authState').textContent='Sign in with an authorized administrator account and complete Google Authenticator MFA.'}
function adminLogout(){for(const k of ['ait_admin_access_token','ait_admin_refresh_token','ait_admin_token','ait_admin_mfa_verified','ait_approver_token'])sessionStorage.removeItem(k);window.adminMfaFactorId=null;window.adminMfaChallengeId=null;$('adminId').value='';$('releaseId').value='';$('approverToken').value='';$('releaseToken').value='';$('releaseSignature').value='';$('withdrawalRows').replaceChildren();$('adminLogoutBtn').hidden=true;$('operatorIdentity').textContent='Signed out. Sign in as a different authorized user to change operator identity.';$('authState').textContent='Signed out.';$('mode').textContent='SIGN IN REQUIRED'}
document.addEventListener('click',event=>{const el=event.target.closest('[data-action]');if(!el)return;const id=Number(el.dataset.withdrawalId);const actions={'toggle-nav':()=>document.body.classList.toggle('nav-open'),'admin-login':adminLogin,'admin-enroll':adminEnroll,'admin-mfa-verify':adminMfaVerify,'admin-logout':adminLogout,'train':train,'backtest':backtest,'strategy-backtest':strategyBacktest,'backtest-all':backtestAll,'ai-review':aiReview,'paper-signals':paperSignals,'signal':signal,'strategy-signal':strategySignal,'paper-step':paperStep,'halt':halt,'reset-risk':resetRisk,'disable-live':disableLive,'load-margin':loadMargin,'load-withdrawals':loadWithdrawals,'load-roles':loadRoles,'save-role':saveRole,'reconcile':reconcile,'approve-withdrawal':()=>approveWithdrawal(id),'reject-withdrawal':()=>rejectWithdrawal(id),'execute-withdrawal':()=>executeWithdrawal(id),'prepare-proposal':()=>prepareWithdrawalProposal(id),'reconcile-withdrawal':()=>reconcileWithdrawal(id)};const fn=actions[el.dataset.action];if(fn){event.preventDefault();fn()}});
document.addEventListener('change',event=>{if(event.target.dataset.actionChange==='load-withdrawals')loadWithdrawals()});
setInterval(()=>{if(loggedIn())refresh()},5000);setInterval(()=>{if(loggedIn())loadWithdrawals()},10000)
