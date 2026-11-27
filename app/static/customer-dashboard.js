
function clearAndAppend(el, nodes){el.replaceChildren(...nodes);}
function cardNode(parts){const el=document.createElement('div');el.className='card';for(const part of parts){const n=document.createElement(part.tag||'div');n.textContent=part.text==null?'':String(part.text);if(part.className)n.className=part.className;el.appendChild(n);}return el;}

let loginMode=false;
const tokenKey='atlas_customer_access_token';
function toggleMode(){loginMode=!loginMode; document.getElementById('authTitle').textContent=loginMode?'Sign in':'Create your account'; document.getElementById('submit').textContent=loginMode?'Sign in':'Create account'; document.getElementById('password').autocomplete=loginMode?'current-password':'new-password'; document.getElementById('toggle').textContent=loginMode?'Create a new account':'I already have an account';}
function formatApiError(value){if(Array.isArray(value))return value.map(item=>{if(!item||typeof item!=='object')return String(item);const loc=Array.isArray(item.loc)?item.loc.slice(1).join('.'):'';const msg=String(item.msg||item.message||JSON.stringify(item));return [loc,msg].filter(Boolean).join(': ')}).join('; ');if(value&&typeof value==='object')return String(value.message||JSON.stringify(value));return value?String(value):'';}
async function api(path, opts={}){opts.headers=Object.assign({'Content-Type':'application/json'},opts.headers||{});const t=sessionStorage.getItem(tokenKey);if(t)opts.headers.Authorization='Bearer '+t;const r=await fetch(path,opts);const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(formatApiError(d.detail)||formatApiError(d.message)||'Request failed');return d;}
async function submitAuth(){const email=document.getElementById('email').value.trim(),password=document.getElementById('password').value;try{const d=await api(loginMode?'/api/auth/login':'/api/auth/signup',{method:'POST',body:JSON.stringify({email,password})});if(loginMode){sessionStorage.setItem(tokenKey,d.access_token);await continueLoginSecurity();}else{document.getElementById('authOut').textContent=d.access_token?'Account created. You can sign in now.':'Account created. Check your email to verify it, then sign in.';}}catch(e){document.getElementById('authOut').textContent=e.message;}}
async function continueLoginSecurity(){try{const s=await api('/api/auth/mfa/status');if(s.enabled && s.aal==='aal2'){showAccount();return}if(s.enabled){await startMfaChallenge();return}await startMfaEnrollment();}catch(e){document.getElementById('authOut').textContent=e.message;sessionStorage.removeItem(tokenKey);}}
async function startMfaEnrollment(){document.getElementById('mfaBox').style.display='block';document.getElementById('mfaTitle').textContent='Enable Google Authenticator';document.getElementById('mfaHelp').textContent='Scan the QR code in Google Authenticator, then enter the 6-digit code it generates. The secret is not stored by Atlas; Supabase Auth manages the TOTP factor.';try{const d=await api('/api/auth/mfa/enroll',{method:'POST'});window.mfaFactorId=d.id;const qr=document.getElementById('mfaQr');qr.replaceChildren();if(d.totp&&d.totp.qr_code){const img=document.createElement('img');img.alt='Google Authenticator QR code';img.style='max-width:320px;height:auto';img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(String(d.totp.qr_code));qr.appendChild(img);}document.getElementById('mfaSecret').textContent=d.totp&&d.totp.secret?d.totp.secret:'Unavailable';document.getElementById('mfaOut').textContent='Scan the QR code, then verify the code.';}catch(e){document.getElementById('mfaOut').textContent=e.message;}}
async function startMfaChallenge(){document.getElementById('mfaBox').style.display='block';document.getElementById('mfaTitle').textContent='Verify Google Authenticator';document.getElementById('mfaHelp').textContent='Enter the current 6-digit code from your Google Authenticator app.';try{const s=await api('/api/auth/mfa/status');window.mfaFactorId=s.factor_count? s.factor_ids[0]:null;if(!window.mfaFactorId)throw new Error('No verified authenticator factor is available.');const d=await api('/api/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:window.mfaFactorId})});window.mfaChallengeId=d.id;document.getElementById('mfaOut').textContent='Authenticator challenge ready.';}catch(e){document.getElementById('mfaOut').textContent=e.message;}}
async function verifyMfa(){const code=document.getElementById('mfaCode').value.trim();if(!/^[0-9]{6}$/.test(code)){document.getElementById('mfaOut').textContent='Enter the 6-digit authenticator code.';return}try{if(!window.mfaChallengeId){const c=await api('/api/auth/mfa/challenge',{method:'POST',body:JSON.stringify({factor_id:window.mfaFactorId})});window.mfaChallengeId=c.id}const d=await api('/api/auth/mfa/verify',{method:'POST',body:JSON.stringify({factor_id:window.mfaFactorId,challenge_id:window.mfaChallengeId,code})});if(d.access_token)sessionStorage.setItem(tokenKey,d.access_token);window.mfaChallengeId=null;document.getElementById('mfaBox').style.display='none';document.getElementById('mfaCode').value='';showAccount();}catch(e){window.mfaChallengeId=null;document.getElementById('mfaOut').textContent=e.message;}}
async function resetPassword(){const email=document.getElementById('email').value.trim();if(!email)return document.getElementById('authOut').textContent='Enter your email first.';try{await api('/api/auth/password-reset',{method:'POST',body:JSON.stringify({email})});document.getElementById('authOut').textContent='Password reset instructions requested.';}catch(e){document.getElementById('authOut').textContent=e.message;}}
async function loadBilling(){try{const d=await api('/api/customer/billing');document.getElementById('customerPlan').textContent=(d.plan.name||d.plan.code).toUpperCase();document.getElementById('customerPlanStatus').textContent=d.subscription.status+' · '+d.subscription.interval;const plans=await api('/api/plans');const box=document.getElementById('billingPlans');box.replaceChildren();for(const p of plans){const card=document.createElement('div');card.className='card';const name=document.createElement('span');name.textContent=p.name;const price=document.createElement('strong');price.textContent=p.monthly===0?'Free plan':'Catalog only · no online payment';const feat=document.createElement('small');feat.textContent=p.code==='pro'?'7-day trial available':(p.features||[]).slice(0,3).join(' · ');card.append(name,price,feat);const note=document.createElement('small');note.textContent=p.code==='free'?'Paper trading and market analysis':'Plan changes are provisioned by an administrator; checkout is disabled.';card.appendChild(note);box.appendChild(card);}document.getElementById('billingOut').textContent=`Referral code: ${d.referral.code||'not created'} · Referred: ${d.referral.referred_count} · Online payment and commission payouts are disabled.`;}catch(e){document.getElementById('billingOut').textContent=e.message;}}
async function createReferralCode(){try{const d=await api('/api/customer/referral/code',{method:'POST'});document.getElementById('billingOut').textContent=`Your referral code: ${d.code}`;document.getElementById('referralCode').value=d.code;await loadBilling();}catch(e){document.getElementById('billingOut').textContent=e.message;}}
async function claimReferral(){const code=document.getElementById('referralCode').value.trim();if(!code){document.getElementById('billingOut').textContent='Enter a referral code first.';return}try{const d=await api('/api/customer/referral/claim',{method:'POST',body:JSON.stringify({code})});document.getElementById('billingOut').textContent=`Referral ${d.code} applied · status ${d.status}`;await loadBilling();}catch(e){document.getElementById('billingOut').textContent=e.message;}}

const BOT_MARKETS={crypto:[['BTC/USDT:USDT','BTC/USDT'],['ETH/USDT:USDT','ETH/USDT'],['BNB/USDT:USDT','BNB/USDT'],['SOL/USDT:USDT','SOL/USDT'],['XRP/USDT:USDT','XRP/USDT'],['ADA/USDT:USDT','ADA/USDT'],['DOGE/USDT:USDT','DOGE/USDT']],forex:[['EUR_USD','EUR/USD'],['GBP_USD','GBP/USD'],['USD_JPY','USD/JPY'],['USD_CHF','USD/CHF'],['AUD_USD','AUD/USD'],['USD_CAD','USD/CAD'],['NZD_USD','NZD/USD']],commodity:[['XAU_USD','Gold / USD'],['XAG_USD','Silver / USD'],['WTICO_USD','WTI Oil / USD'],['BCO_USD','Brent / USD'],['NATGAS_USD','Natural Gas / USD']]};
function updateBotMarkets(){const asset=document.getElementById('botAsset').value;const sel=document.getElementById('botSymbol');sel.replaceChildren();for(const [v,t] of (BOT_MARKETS[asset]||[])){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o);}}
async function connectDeriv(){const out=document.getElementById('botOut');try{const d=await api('/api/customer/broker/deriv/connect',{method:'POST',body:JSON.stringify({account_id:document.getElementById('derivAccountId').value.trim(),api_token:document.getElementById('derivToken').value})});document.getElementById('botController').textContent=`Deriv: ${d.status} · access verified · live trading disabled`;document.getElementById('derivToken').value='';out.textContent=JSON.stringify(d,null,2);}catch(e){out.textContent=e.message;}}
async function preflightDeriv(){const out=document.getElementById('botOut');try{const d=await api('/api/customer/deriv/preflight',{method:'POST',body:JSON.stringify({})});out.textContent=JSON.stringify(d,null,2);}catch(e){out.textContent=e.message;}}
async function connectOanda(){const out=document.getElementById('botOut');try{const d=await api('/api/customer/broker/oanda/connect',{method:'POST',body:JSON.stringify({account_id:document.getElementById('oandaAccountId').value.trim(),api_token:document.getElementById('oandaToken').value,practice:true})});document.getElementById('botController').textContent=`OANDA: ${d.status} · OANDA Practice/Demo · Live execution disabled`;document.getElementById('oandaToken').value='';out.textContent=JSON.stringify(d,null,2);}catch(e){out.textContent=e.message;}}
updateBotMarkets();
async function startBot(){const out=document.getElementById('botOut');const plan=document.getElementById('botPlan');const learning=document.getElementById('botLearning');out.textContent='Starting the persistent controller and running the first gated cycle…';plan.replaceChildren();learning.textContent='Adaptive learner: walk-forward validation / champion check in progress';try{const d=await api('/api/customer/bot/start',{method:'POST',body:JSON.stringify({asset:document.getElementById('botAsset').value,symbol:document.getElementById('botSymbol').value.trim(),exchange:document.getElementById('botAsset').value==='crypto'?'binance':'oanda',timeframe:document.getElementById('botTimeframe').value,days:365,autonomous:true,interval_seconds:Number(document.getElementById('botInterval').value)})});const a=d.adaptive_model||{};const g=a.gate||a.walk_forward_gate||{};const fd=a.feature_drift||{};const driftTxt=fd.status&&fd.status!=='NO_BASELINE'&&fd.status!=='INSUFFICIENT_DATA'?` · Market drift ${fd.status}${typeof fd.mean_psi==='number'?' (PSI '+fd.mean_psi.toFixed(2)+')':''}`:'';learning.textContent=`Adaptive learner: ${a.status||'UNKNOWN'} · OOS Sharpe ${Number(g.sharpe||0).toFixed(2)} · Max DD ${(Number(g.max_drawdown||0)*100).toFixed(1)}%${driftTxt}`;document.getElementById('botController').textContent=`Controller: ${d.autonomous_status||'RUNNING'} · Bot #${d.autonomous_bot_id||'—'}`;if(d.trade_plan){const p=d.trade_plan;for(const item of [['ENTRY',p.entry_price],['STOP LOSS',p.stop_loss_price],['TAKE PROFIT',p.take_profit_price]]){const div=document.createElement('div');const span=document.createElement('span');span.textContent=item[0];const b=document.createElement('b');b.textContent=Number(item[1]).toFixed(6);div.append(span,b);plan.appendChild(div);}}out.textContent=JSON.stringify(d,null,2);await refreshBotStatus();}catch(e){learning.textContent='Adaptive learner: unavailable';out.textContent=e.message;}}
function setAutomationStatus(mode,message){const el=document.getElementById('automationStatus');if(!el)return;const m=String(mode||'PAPER').toUpperCase();el.className='automation-status '+(m==='LIVE'?'live':'paper');el.replaceChildren();const strong=document.createElement('strong');strong.textContent=m==='LIVE'?'Current mode: LIVE AUTOMATED':'Current mode: PAPER';const span=document.createElement('span');span.textContent=message;el.append(strong,span);} async function requestLiveExecutor(){const ok=window.confirm('Enable live automation for this executor? Atlas may place real orders automatically without asking you to confirm each individual slice. Live execution remains subject to the platform kill switch, customer/live-trading gates, entitlement, broker checks and risk controls.');if(!ok)return;await createExecutor('LIVE');} async function refreshBotStatus(){try{const rows=await api('/api/customer/bot');const b=rows&&rows[0];if(!b){document.getElementById('botController').textContent='Controller: not configured';setAutomationStatus('PAPER','Atlas is ready for paper automation. No live automation is active.');return;}document.getElementById('botController').textContent=`Controller: ${b.status} · ${b.mode} · cycle ${Math.round(Number(b.interval_seconds||900)/60)}m · last ${b.last_decision||'NONE'}`;setAutomationStatus(String(b.mode||'PAPER'),b.status==='RUNNING'&&String(b.mode||'').toUpperCase()==='LIVE'?'Atlas can place real orders automatically while the live gates remain satisfied.':String(b.mode||'').toUpperCase()==='LIVE'?'Live automation is configured but the controller is not currently running.':'Atlas may evaluate markets and simulate trades automatically. No real orders are being placed.');}catch(e){document.getElementById('botController').textContent='Controller: status unavailable';setAutomationStatus('PAPER','Automation status is temporarily unavailable.');}}
async function stopAutonomousBot(){try{const rows=await api('/api/customer/bot');if(rows&&rows[0]){await api(`/api/customer/bot/${rows[0].id}/action`,{method:'POST',body:JSON.stringify({action:'STOP'})});}document.getElementById('botController').textContent='Controller: STOPPED';}catch(e){document.getElementById('botOut').textContent=e.message;}}
async function loadProductWorkspace(){try{const p=await api('/api/customer/portfolio');document.getElementById('portfolioOut').textContent=`Equity: $${Number(p.equity).toFixed(2)} · Cash: $${Number(p.cash_equity).toFixed(2)} · Positions: ${p.positions.length}`;const s=await api('/api/customer/market-scanner?exchange=bybit&symbols=BTC%2FUSDT%3AUSDT%2CETH%2FUSDT%3AUSDT%2CSOL%2FUSDT%3AUSDT');const out=document.getElementById('scannerOut');out.replaceChildren();for(const x of (s.results||[])){const div=document.createElement('div');const b=document.createElement('b');b.textContent=x.symbol||'';div.appendChild(b);div.append(` ${x.signal||''} · score ${Number(x.score||0).toFixed(2)} · RSI ${Number(x.rsi||0).toFixed(1)}`);out.appendChild(div);}}catch(e){document.getElementById('scannerOut').textContent=e.message;document.getElementById('portfolioOut').textContent=e.message;}}
async function createSmartTrade(){try{const d=await api('/api/customer/smart-trades',{method:'POST',body:JSON.stringify({symbol:document.getElementById('botSymbol').value.trim(),exchange:'bybit',side:document.getElementById('stSide').value,entry_price:Number(document.getElementById('stEntry').value),quantity:Number(document.getElementById('stQty').value),stop_loss_price:Number(document.getElementById('stStop').value),take_profit_1:Number(document.getElementById('stTp1').value)})});document.getElementById('smartOut').textContent=JSON.stringify(d,null,2);}catch(e){document.getElementById('smartOut').textContent=e.message;}}
async function createDcaBot(){try{const d=await api('/api/customer/dca-bots',{method:'POST',body:JSON.stringify({symbol:document.getElementById('botSymbol').value.trim(),exchange:'bybit',initial_quote:Number(document.getElementById('dcaInitial').value),safety_order_quote:Number(document.getElementById('dcaSafety').value),max_safety_orders:Number(document.getElementById('dcaOrders').value),deviation_pct:Number(document.getElementById('dcaDeviation').value)})});document.getElementById('dcaOut').textContent=JSON.stringify(d,null,2);}catch(e){document.getElementById('dcaOut').textContent=e.message;}}
function fmtPct(x){return (typeof x==='number'&&isFinite(x))?(x*100).toFixed(1)+'%':'n/a';}
function fmtNum(x){return (typeof x==='number'&&isFinite(x))?x.toFixed(2):'n/a';}
function summarizeValidation(d){const g=d.gate||{};const lines=[`Result: ${d.status}${d.attempt?` (attempt ${d.attempt})`:''}`,`Strategy family tested: ${g.strategy_family||'n/a'} -> Atlas component "${g.validated_component||'n/a'}"`,`Sharpe ${fmtNum(g.sharpe)} | Max drawdown ${fmtPct(g.max_drawdown)} | Trades ${g.trades??'n/a'} | Return ${fmtPct(g.total_return)}`,`Out-of-sample return ${fmtPct(g.oos_total_return)} | Positive folds ${fmtPct(g.oos_positive_fold_ratio)}`];if(d.validation_scope)lines.push('Scope: '+d.validation_scope);lines.push('Live trading still requires your explicit approval.');return lines.join('\n');}
async function buildStrategy(){
  const out=document.getElementById('strategyOut');
  const consent=document.getElementById('strategyBenchmarkOptIn');
  const benchmarkOptIn=!!consent?.checked;
  if(consent)consent.checked=false;
  try{
    out.textContent='Generating strategy...';
    const c=await api('/api/customer/strategy-builder',{
      method:'POST',
      body:JSON.stringify({
        name:'Atlas Strategy Draft',
        prompt:document.getElementById('strategyPrompt').value,
        benchmark_opt_in:benchmarkOptIn
      })
    });
    const capture=c.benchmark_capture?.status==='STORED'
      ?' Opted-in prompt and Native AI output were encrypted for up to 90 days.'
      :c.benchmark_capture?.status&&c.benchmark_capture.status!=='NOT_REQUESTED'
        ?' Benchmark capture was not stored ('+c.benchmark_capture.status+').':'';
    out.textContent='Strategy created (candidate #'+c.candidate_id+').'+capture+' Validating...';
    const v=await api('/api/customer/strategy-lab/candidates/'+c.candidate_id+'/validate',{method:'POST'});
    out.textContent=summarizeValidation(v)+capture;
    await loadCandidates();
    await loadBenchmarkCaptureStatus();
  }catch(e){
    out.textContent=e.message;
  }
}
document.getElementById('strategyPrompt')?.addEventListener('input',()=>{
  const consent=document.getElementById('strategyBenchmarkOptIn');
  if(consent)consent.checked=false;
});

async function loadDeposit(){try{const d=await api('/api/customer/usdt/deposit');const box=document.getElementById('depositBox');box.replaceChildren();const card=document.createElement('div');card.className='card';for(const [label,value] of [['USDT NETWORK','TRON (TRC-20)'],['Deposit address',d.deposit_address]]){const small=document.createElement('small');small.textContent=label;card.appendChild(small);if(label==='Deposit address'){const addr=document.createElement('div');addr.style='word-break:break-all;font-family:monospace';addr.textContent=d.deposit_address;card.appendChild(addr);}}const btn=document.createElement('button');btn.className='secondary';btn.textContent='Copy address';btn.addEventListener('click',()=>navigator.clipboard.writeText(String(d.deposit_address||'')));card.appendChild(btn);box.appendChild(card);}catch(e){document.getElementById('depositBox').textContent=e.message;}}
async function sendWithdrawalOtp(){try{const email=document.getElementById('otpEmail').value.trim(),amount=Number(document.getElementById('wdAmount').value),destination=document.getElementById('wdDestination').value.trim();if(!amount||!destination){document.getElementById('otpOut').textContent='Enter the withdrawal amount and TRON destination before requesting OTP.';return}const d=await api('/api/auth/otp/send',{method:'POST',body:JSON.stringify({email,create_user:false,purpose:'withdrawal',amount,destination,network:'TRON'})});sessionStorage.setItem('atlas_withdrawal_otp_intent',d.otp_intent);document.getElementById('otpOut').textContent='OTP sent. Verify the code for this exact withdrawal request.';}catch(e){document.getElementById('otpOut').textContent=e.message;}}
async function verifyWithdrawalOtp(){try{const email=document.getElementById('otpEmail').value.trim(),token=document.getElementById('withdrawalOtp').value.trim(),amount=Number(document.getElementById('wdAmount').value),destination=document.getElementById('wdDestination').value.trim(),intent=sessionStorage.getItem('atlas_withdrawal_otp_intent')||'';if(!amount||!destination||!intent){document.getElementById('otpOut').textContent='Request a fresh OTP for the exact withdrawal details first.';return}const d=await api('/api/auth/otp/verify',{method:'POST',body:JSON.stringify({email,token,purpose:'withdrawal',amount,destination,network:'TRON',otp_intent_token:intent})});sessionStorage.setItem('atlas_withdrawal_step_up',d.step_up_token);sessionStorage.removeItem('atlas_withdrawal_otp_intent');document.getElementById('otpOut').textContent='Withdrawal verification active for '+Math.round(d.expires_in/60)+' minutes.';}catch(e){document.getElementById('otpOut').textContent=e.message;}}
async function requestWithdrawal(){const amount=Number(document.getElementById('wdAmount').value),destination=document.getElementById('wdDestination').value.trim();if(!amount||!destination){document.getElementById('wdOut').textContent='Enter an amount and TRON address.';return}if(!confirm(`Request ${amount.toFixed(6)} USDT to ${destination.slice(0,8)}…${destination.slice(-6)}?`))return;try{const idemPayload=JSON.stringify({amount,destination}),savedPayload=sessionStorage.getItem('atlas_withdrawal_idem_payload');let idemKey=sessionStorage.getItem('atlas_withdrawal_idem');if(!idemKey||savedPayload!==idemPayload){idemKey=crypto.randomUUID();sessionStorage.setItem('atlas_withdrawal_idem',idemKey);sessionStorage.setItem('atlas_withdrawal_idem_payload',idemPayload);}const d=await api('/api/customer/withdrawals',{method:'POST',headers:{'X-Withdrawal-Step-Up':sessionStorage.getItem('atlas_withdrawal_step_up')||'','Idempotency-Key':idemKey},body:JSON.stringify({amount,destination,network:'TRON'})});document.getElementById('wdOut').textContent=JSON.stringify(d,null,2);document.getElementById('wdAmount').value='';document.getElementById('wdDestination').value='';sessionStorage.removeItem('atlas_withdrawal_step_up');sessionStorage.removeItem('atlas_withdrawal_otp_intent');sessionStorage.removeItem('atlas_withdrawal_idem');sessionStorage.removeItem('atlas_withdrawal_idem_payload');document.getElementById('withdrawalOtp').value='';document.getElementById('otpOut').textContent='Verification consumed. A new OTP is required for another withdrawal.';await loadAccount();}catch(e){document.getElementById('wdOut').textContent=e.message;}}
async function loadWithdrawals(){try{const rows=await api('/api/customer/withdrawals');const box=document.getElementById('withdrawals');box.replaceChildren();if(!rows.length){const p=document.createElement('p');p.className='muted';p.textContent='No withdrawal requests yet.';box.appendChild(p);return;}for(const w of rows){const card=document.createElement('div');card.className='card';const b=document.createElement('b');b.textContent=`${Number(w.amount).toFixed(6)} ${w.currency}`;const n=document.createElement('small');n.textContent=`${w.network} · ${w.destination_masked}`;const st=document.createElement('small');st.textContent=`Status: ${w.status} · Approval ${w.approval_count}/${w.required_approvals}`;card.append(b,n,st);if(w.rejection_reason){const r=document.createElement('small');r.textContent=`Reason: ${w.rejection_reason}`;card.appendChild(r);}box.appendChild(card);}}catch(e){document.getElementById('withdrawals').textContent=e.message;}}
async function loadAccount(){try{const me=await api('/api/customer/me');document.getElementById('customerEmail').textContent=me.email||'Customer';document.getElementById('customerStatus').textContent=me.status;const wallets=await api('/api/customer/wallets');let total=0;const walletBox=document.getElementById('wallets');walletBox.replaceChildren();if(wallets.length){for(const w of wallets){total+=Number(w.available_balance);const card=document.createElement('div');card.className='card';const b=document.createElement('b');b.textContent=w.currency;const st=document.createElement('strong');st.textContent=Number(w.available_balance).toFixed(6);const sm=document.createElement('small');sm.textContent=`${w.wallet_type} · ${w.status} · ${w.network||''}`;card.append(b,st,sm);walletBox.appendChild(card);}}else{const p=document.createElement('p');p.className='muted';p.textContent='No trading wallet yet.';walletBox.appendChild(p);}document.getElementById('walletTotal').textContent=total.toFixed(6);const fs=await api('/api/customer/funding');const fundingBox=document.getElementById('funding');fundingBox.replaceChildren();if(fs.length){for(const f of fs){const card=document.createElement('div');card.className='card';const b=document.createElement('b');b.textContent=`${f.currency} ${Number(f.amount).toFixed(6)}`;const sm=document.createElement('small');sm.textContent=`${f.provider} · ${f.status}`;card.append(b,sm);fundingBox.appendChild(card);}}else{const p=document.createElement('p');p.className='muted';p.textContent='No funding transactions yet.';fundingBox.appendChild(p);}await loadDeposit();await loadWithdrawals();await loadBenchmarkCaptureStatus();}catch(e){sessionStorage.removeItem(tokenKey);document.getElementById('authOut').textContent=e.message;document.getElementById('auth').style.display='block';document.getElementById('account').style.display='none';}}
async function logout(){try{await api('/api/auth/logout',{method:'POST'});}catch(e){}sessionStorage.removeItem(tokenKey);location.reload();}
if(sessionStorage.getItem(tokenKey))continueLoginSecurity();


// Atlas UI layer: responsive navigation and account-synced display preferences.
const uiSections={overview:['Your command center','Monitor balances, markets and customer-scoped trading tools.'],markets:['Market overview','Follow Atlas signals and customer-scoped market intelligence.'],trade:['Trade desk','Build paper trades and analyze setups before execution.'],wallet:['Wallet','Fund, monitor and protect your USDT trading wallet.'],security:['Security','Manage withdrawal verification and account protection.'],settings:['Settings','Preferences, billing, referrals and session controls.']};
function setActiveSection(id){document.querySelectorAll('.nav-link').forEach(a=>a.classList.toggle('active',a.dataset.section===id));const meta=uiSections[id];if(meta){document.getElementById('pageTitle').textContent=meta[0];document.getElementById('pageSubtitle').textContent=meta[1];}}
document.querySelectorAll('.nav-link').forEach(a=>a.addEventListener('click',()=>{setActiveSection(a.dataset.section);document.getElementById('appShell').classList.remove('nav-open');}));
let activeMarketFilter='all';
function applyMarketFilters(){const query=(document.getElementById('globalSearch')?.value||'').trim().toLowerCase();document.querySelectorAll('#marketRows tr').forEach(tr=>{const cells=[...tr.cells].map(c=>c.textContent.toLowerCase());const signal=cells[1]||'';const matchesSearch=!query||cells.join(' ').includes(query);const matchesFilter=activeMarketFilter==='all'||(activeMarketFilter==='bullish'&&/long|buy|bullish/.test(signal))||(activeMarketFilter==='bearish'&&/short|sell|bearish/.test(signal));tr.hidden=!(matchesSearch&&matchesFilter);});}
document.getElementById('globalSearch')?.addEventListener('input',applyMarketFilters);
document.querySelectorAll('[data-market-filter]').forEach(btn=>btn.addEventListener('click',()=>{activeMarketFilter=btn.dataset.marketFilter||'all';document.querySelectorAll('[data-market-filter]').forEach(b=>b.classList.toggle('active',b===btn));applyMarketFilters();}));
document.getElementById('menuBtn')?.addEventListener('click',()=>document.getElementById('appShell').classList.toggle('nav-open'));
window.addEventListener('hashchange',()=>setActiveSection(location.hash.replace('#','')||'overview'));
if(location.hash)setActiveSection(location.hash.replace('#',''));
const WORKSPACE_SETTING_FIELDS={reduceMotion:'reduce_motion',compactMarkets:'compact_markets',touchFeedback:'touch_feedback'};
/*
 * Older builds used origin-wide localStorage keys for account preferences. Those
 * values are shared by every customer in this browser, so remove them and never
 * import or cache them. The signed-in account's API row is the only source of truth.
 */
try{
  for(const setting of Object.keys(WORKSPACE_SETTING_FIELDS)){
    localStorage.removeItem(`atlas_ui_${setting}`);
  }
}catch(_storageError){}
let workspacePreferenceSaveQueue=Promise.resolve();
function applyWorkspacePreference(el,value){
  el.checked=!!value;
  if(el.id==='reduceMotion')document.body.classList.toggle('reduce-motion',el.checked);
  if(el.id==='compactMarkets')document.body.classList.toggle('compact-markets',el.checked);
  if(el.id==='touchFeedback')document.body.classList.toggle('touch-feedback-enabled',el.checked);
}
function applyWorkspacePreferences(preferences){
  document.querySelectorAll('[data-setting]').forEach(el=>{
    const field=WORKSPACE_SETTING_FIELDS[el.dataset.setting];
    if(field&&Object.prototype.hasOwnProperty.call(preferences||{},field)){
      applyWorkspacePreference(el,preferences[field]);
    }
  });
}
const DEFAULT_WORKSPACE_PREFERENCES={
  reduce_motion:false,
  compact_markets:false,
  touch_feedback:true
};
async function loadWorkspacePreferences(){
  try{
    const saved=await api('/api/customer/workspace-preferences');
    applyWorkspacePreferences(saved.preferences||DEFAULT_WORKSPACE_PREFERENCES);
    const status=document.getElementById('workspacePreferencesStatus');
    if(status)status.textContent=saved.configured
      ? 'Saved to your Atlas account.'
      : 'Using your account defaults.';
  }catch(error){
    // A failed account read must not reuse the previous customer's browser state.
    applyWorkspacePreferences(DEFAULT_WORKSPACE_PREFERENCES);
    const status=document.getElementById('workspacePreferencesStatus');
    if(status)status.textContent='Account preferences could not be loaded; safe defaults are shown.';
  }
}
document.querySelectorAll('[data-setting]').forEach(el=>{
  el.addEventListener('change',()=>{
    const field=WORKSPACE_SETTING_FIELDS[el.dataset.setting];
    if(!field)return;
    applyWorkspacePreference(el,el.checked);
    if(!sessionStorage.getItem(tokenKey))return;
    const value=el.checked;
    workspacePreferenceSaveQueue=workspacePreferenceSaveQueue.catch(()=>{}).then(async()=>{
      await api('/api/customer/workspace-preferences',{
        method:'PATCH',body:JSON.stringify({[field]:value})
      });
      const status=document.getElementById('workspacePreferencesStatus');
      if(status)status.textContent='Saved to your Atlas account.';
    }).catch(()=>{
      const status=document.getElementById('workspacePreferencesStatus');
      if(status)status.textContent='Could not sync this preference. The saved account value will be restored on next load.';
    });
  });
});
/* Keep one wrapper per lifecycle function: predictable, testable, and easy to maintain. */
async function showAccount(){
  document.getElementById('auth').style.display='none';
  document.getElementById('mfaBox').style.display='none';
  document.getElementById('account').style.display='block';
  await loadAccount();
  await loadWorkspacePreferences();
  await loadProductWorkspace();
  for(const fn of [loadBilling,loadCandidates,loadExecutors,refreshBotStatus]){try{await fn();}catch(e){}}
  syncUiAccount();
}
function syncUiAccount(){
  const email=document.getElementById('customerEmail')?.textContent||'—';
  const status=document.getElementById('customerStatus')?.textContent||'—';
  const se=document.getElementById('settingsEmail'), ss=document.getElementById('settingsStatus');
  if(se)se.textContent=email;
  if(ss)ss.textContent=status;
  const wb=document.getElementById('walletTotal')?.textContent||'—';
  const wh=document.getElementById('walletBalanceHero');
  if(wh)wh.textContent=wb;
}
const _loadAccount=loadAccount;
loadAccount=async function(){
  await _loadAccount();
  syncUiAccount();
  try{
    const p=await api('/api/customer/portfolio');
    document.getElementById('snapshotEquity').textContent='$'+Number(p.equity||0).toFixed(2);
    document.getElementById('snapshotCash').textContent='$'+Number(p.cash_equity||0).toFixed(2);
    document.getElementById('snapshotPositions').textContent=String((p.positions||[]).length);
    const po=document.getElementById('portfolioOut');
    if(po)po.textContent='Equity: $'+Number(p.equity||0).toFixed(2)+' · Cash: $'+Number(p.cash_equity||0).toFixed(2)+' · Positions: '+((p.positions||[]).length);
  }catch(e){}
  await renderHome().catch(()=>{});
};
const _loadProductWorkspace=loadProductWorkspace;
loadProductWorkspace=async function(){
  await _loadProductWorkspace();
  const src=document.getElementById('scannerOut'), rows=document.getElementById('marketRows');
  if(src&&rows){
    const items=src.querySelectorAll('div');
    rows.replaceChildren();
    if(items.length){
      for(const x of items){
        const t=x.textContent.trim().split(' ');
        const tr=document.createElement('tr');
        for(const val of [t[0]||'—',t[1]||'—',t[4]||'—',t[7]||'—','READY']){
          const td=document.createElement('td');td.textContent=val;tr.appendChild(td);
        }
        rows.appendChild(tr);
      }
      applyMarketFilters();
    }else{
      const tr=document.createElement('tr'),td=document.createElement('td');
      td.colSpan=5;td.className='muted';td.textContent='No scanner results.';
      tr.appendChild(td);rows.appendChild(tr);
    }
  }
  await renderWatch().catch(()=>{});
};
const _loadDeposit=loadDeposit;
loadDeposit=async function(){await _loadDeposit();document.getElementById('depositBox')?.classList.add('loaded');};

async function createLabCandidate(){
  const consent=document.getElementById('labBenchmarkOptIn');
  const benchmarkOptIn=!!consent?.checked;
  if(consent)consent.checked=false;
  try{
    const d=await api('/api/customer/strategy-lab/candidates',{
      method:'POST',
      body:JSON.stringify({
        name:document.getElementById('labName').value,
        prompt:document.getElementById('labPrompt').value,
        asset:document.getElementById('labAsset').value,
        symbol:document.getElementById('labSymbol').value,
        exchange:document.getElementById('labAsset').value==='crypto'?'binance':'oanda',
        timeframe:document.getElementById('labTf').value||null,
        days:365,
        benchmark_opt_in:benchmarkOptIn
      })
    });
    const capture=d.benchmark_capture?.status==='STORED'
      ?'This submission was encrypted and stored for up to 90 days.'
      :d.benchmark_capture?.status&&d.benchmark_capture.status!=='NOT_REQUESTED'
        ?'This submission was not stored ('+d.benchmark_capture.status+').'
        :'Benchmark capture was not requested for this submission.';
    const captureResult=document.getElementById('labBenchmarkCaptureResult');
    if(captureResult)captureResult.textContent=capture;
    document.getElementById('labOut').textContent='Candidate #'+d.id+' created. Validating...';
    await validateCandidate(d.id);
    await loadBenchmarkCaptureStatus();
  }catch(e){
    const captureResult=document.getElementById('labBenchmarkCaptureResult');
    if(captureResult&&benchmarkOptIn)captureResult.textContent='Capture status could not be confirmed for this submission.';
    document.getElementById('labOut').textContent=e.message;
  }
}
document.getElementById('labPrompt')?.addEventListener('input',()=>{
  const consent=document.getElementById('labBenchmarkOptIn');
  if(consent)consent.checked=false;
});

async function loadBenchmarkCaptureStatus(){const el=document.getElementById('benchmarkCaptureStatus');if(!el)return;try{const d=await api('/api/customer/benchmark-captures');if(d.status!=='AVAILABLE'){el.textContent='Benchmark submission status is unavailable in this read-only runtime.';return;}el.textContent=d.count+' saved benchmark submission(s); each expires after '+d.retention_days+' days.';}catch(e){el.textContent='Benchmark submission status unavailable.';}}
async function deleteBenchmarkCaptures(){const out=document.getElementById('benchmarkCaptureStatus');if(!window.confirm('Permanently delete all of your saved benchmark submissions? This cannot be undone.'))return;try{const d=await api('/api/customer/benchmark-captures',{method:'DELETE'});if(out)out.textContent='Deleted '+d.deleted+' benchmark submission(s).';}catch(e){if(out)out.textContent=e.message;}}
async function validateCandidate(id){try{const d=await api(`/api/customer/strategy-lab/candidates/${id}/validate`,{method:'POST'});document.getElementById('labOut').textContent=summarizeValidation(d);await loadCandidates();}catch(e){document.getElementById('labOut').textContent=e.message;}}
async function approveCandidate(id){try{const d=await api(`/api/customer/strategy-lab/candidates/${id}/approve-live`,{method:'POST'});document.getElementById('labOut').textContent=JSON.stringify(d,null,2);await loadCandidates();}catch(e){document.getElementById('labOut').textContent=e.message;}}
async function showCandidateHistory(id){const out=document.getElementById('labOut');try{const rows=await api(`/api/customer/strategy-lab/candidates/${id}/runs`);if(!rows.length){out.textContent='No validation attempts recorded yet.';return;}out.textContent=rows.map(r=>{const g=r.gate||{};const dl=r.delta_vs_previous;const delta=dl?` | vs previous: Sharpe ${dl.sharpe>=0?'+':''}${fmtNum(dl.sharpe)}, return ${dl.total_return>=0?'+':''}${fmtPct(dl.total_return)}`:'';return `Attempt ${r.attempt} (${r.created_at.slice(0,16).replace('T',' ')}): ${r.passed?'PASSED':'REJECTED'} | Sharpe ${fmtNum(g.sharpe)} | DD ${fmtPct(g.max_drawdown)} | Return ${fmtPct(g.total_return)}${delta}`;}).join('\n');}catch(e){out.textContent=e.message;}}
async function loadCandidates(){try{const rows=await api('/api/customer/strategy-lab/candidates');const box=document.getElementById('candidateList');box.replaceChildren();for(const r of rows){const div=document.createElement('div');div.className='activity-item';const title=document.createElement('b');title.textContent=`${r.name} · ${r.symbol}`;const meta=document.createElement('span');meta.textContent=`${r.status} · ${r.live_approved?'LIVE APPROVED':'not live approved'}`;div.append(title,meta);if(r.status==='DRAFT'||r.status==='REJECTED'){const b=document.createElement('button');b.textContent='Validate';b.onclick=()=>validateCandidate(r.id);div.append(b);}if(r.status==='VALIDATED'&&!r.live_approved){const b=document.createElement('button');b.textContent='Approve live';b.onclick=()=>approveCandidate(r.id);div.append(b);}if(r.status!=='DRAFT'){const h=document.createElement('button');h.textContent='History';h.onclick=()=>showCandidateHistory(r.id);div.append(h);}box.appendChild(div);}}catch(e){document.getElementById('labOut').textContent=e.message;}}
async function createExecutor(mode){try{const d=await api('/api/customer/executors',{method:'POST',body:JSON.stringify({kind:document.getElementById('execKind').value,asset:document.getElementById('botAsset').value,symbol:document.getElementById('botSymbol').value,exchange:document.getElementById('botAsset').value==='crypto'?'binance':'oanda',timeframe:document.getElementById('botTimeframe').value,side:document.getElementById('execSide').value,total_quantity:Number(document.getElementById('execQty').value),slices:Number(document.getElementById('execSlices').value),interval_seconds:60,max_slippage_bps:25,mode})});document.getElementById('executorOut').textContent=JSON.stringify(d,null,2);await loadExecutors();}catch(e){document.getElementById('executorOut').textContent=e.message;}}
async function loadExecutors(){try{const rows=await api('/api/customer/executors');const box=document.getElementById('executorList');box.replaceChildren();for(const r of rows){const div=document.createElement('div');div.className='activity-item';const title=document.createElement('b');title.textContent=`${r.kind} · ${r.symbol}`;const meta=document.createElement('span');meta.textContent=`${r.status} · ${r.mode} · ${Number(r.executed_quantity).toFixed(6)} / ${Number(r.target_quantity).toFixed(6)}`;div.append(title,meta);if(r.status==='ARMED'){const stop=document.createElement('button');stop.textContent='Stop';stop.onclick=()=>executorAction(r.id,'STOP');div.append(stop);}if(r.mode==='PAPER'&&r.status==='ARMED'){const live=document.createElement('button');live.textContent='Approve live';live.onclick=()=>executorAction(r.id,'APPROVE_LIVE');div.append(live);}box.appendChild(div);}}catch(e){document.getElementById('executorOut').textContent=e.message;}}
/* Home data is API-backed; no synthetic balances or performance values. */
const HOME_USD=new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'});
function fmtUsd(n,signed){const v=Number(n)||0;const s=HOME_USD.format(Math.abs(v));return (v<0?'−':(signed&&v>0?'+':''))+s;} function fmtUsdt(n,signed){const v=Number(n)||0;const s=Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:6})+' USDT';return (v<0?'−':(signed&&v>0?'+':''))+s;} 
function pnlClass(v){return v>0?'pos':(v<0?'neg':'flat');}
function homeName(email){const m=/^[A-Za-z]{2,}/.exec(String(email||'').split('@')[0]);return m?'Welcome, '+m[0][0].toUpperCase()+m[0].slice(1).toLowerCase():'Welcome back';}
function groupLedger(lines){const m=new Map();for(const l of lines){if(!String(l.account_code||'').startsWith('LIABILITY:CUSTOMER:'))continue;let j=m.get(l.journal_id);if(!j){j={id:l.journal_id,type:l.entry_type,at:l.created_at,net:0};m.set(l.journal_id,j);}j.net+=Number(l.credit||0)-Number(l.debit||0);}return [...m.values()].sort((a,b)=>Date.parse(b.at)-Date.parse(a.at));}
const HOME_ACT={DEPOSIT_CREDIT:['Deposit','dep','↓'],TRADE_SETTLEMENT:['Trade result','trd','↔'],TRADING_FEE:['Trading fee','fee','%'],WITHDRAWAL_SETTLEMENT:['Withdrawal sent','wd','↑']};
function homeNote(el,text){el.replaceChildren();const li=document.createElement('li');li.className='empty-note';li.textContent=text;el.appendChild(li);}
function renderActivity(journals){const ul=document.getElementById('homeActivity');const rows=journals.filter(j=>Math.abs(j.net)>1e-9).slice(0,4);if(!rows.length){homeNote(ul,'No activity yet. Deposits, trades and withdrawals will show up here.');return;}ul.replaceChildren();for(const j of rows){const meta=HOME_ACT[j.type]||[String(j.type||'Activity').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase()),'fee','•'];const li=document.createElement('li');li.className='act-row';const b=document.createElement('span');b.className='badge '+meta[1];b.textContent=meta[2];const lbl=document.createElement('span');lbl.className='lbl';lbl.textContent=meta[0];const when=document.createElement('small');when.className='muted';when.textContent=' '+new Date(j.at).toLocaleDateString(undefined,{month:'short',day:'numeric'});lbl.appendChild(when);const amt=document.createElement('span');amt.className='amt '+pnlClass(j.net);amt.textContent=fmtUsd(j.net,true);li.append(b,lbl,amt);ul.appendChild(li);}}
let perfDays = 7;
let cachedJournals = [];

function setPerfTimeframe(days) {
  perfDays = days;
  document.getElementById('perfTitle').textContent = `Realised trading P&L · ${days} days`;
  const btn7 = document.getElementById('btnTf7');
  const btn30 = document.getElementById('btnTf30');
  if (btn7 && btn30) {
    if (days === 7) {
      btn7.style.background = '#10b981'; btn7.style.color = '#fff'; btn7.style.borderColor = '#10b981';
      btn30.style.background = '#fff'; btn30.style.color = '#334155'; btn30.style.borderColor = '#cbd5e1';
    } else {
      btn30.style.background = '#10b981'; btn30.style.color = '#fff'; btn30.style.borderColor = '#10b981';
      btn7.style.background = '#fff'; btn7.style.color = '#334155'; btn7.style.borderColor = '#cbd5e1';
    }
  }
  if (cachedJournals.length) renderPerformance(cachedJournals);
}

function renderPerformance(journals){
  cachedJournals = journals;
  const box=document.getElementById('homeChart'),sub=document.getElementById('perfSub'),cutoff=Date.now()-perfDays*864e5;
  const rows=journals.filter(j=>(j.type==='TRADE_SETTLEMENT'||j.type==='TRADING_FEE')&&Date.parse(j.at)>=cutoff).sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
  box.replaceChildren();
  if(!rows.length){
    sub.textContent='No persisted trade-settlement history is available for this period. Total historical equity is not yet verified.';
    const empty=document.createElement('div');
    empty.className='chart-empty';
    empty.textContent='Verified performance history is not available yet.';
    box.appendChild(empty);
    return;
  }
  let run=0;
  const points=rows.map(j=>{run+=j.net;return {at:j.at,v:run};});
  const total=run,vals=points.map(x=>x.v),min=Math.min(0,...vals),max=Math.max(0,...vals),range=Math.max(max-min,1),w=720,h=210,pad=18;
  sub.textContent='Net realised trading P&L '+fmtUsd(total,true)+' after fees ('+perfDays+' days). Ledger-derived; deposits and withdrawals excluded. This is not total portfolio equity.';
  drawRechartsAreaChart(box, points, min, range, total, w, h, pad);
}

function drawRechartsAreaChart(box, points, min, range, total, w, h, pad) {
  const xy=(x,i)=>{const xx=pad+(i/Math.max(points.length-1,1))*(w-pad*2);const yy=h-pad-((x.v-min)/range)*(h-pad*2);return [xx,yy];};
  const coords = points.map((p, i) => xy(p, i));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 '+w+' '+h);svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');svg.setAttribute('style','width:100%;height:100%;display:block;');
  
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const grad = document.createElementNS('http://www.w3.org/2000/svg', 'linearGradient');
  grad.setAttribute('id', 'rechartsGrad');
  grad.setAttribute('x1', '0'); grad.setAttribute('y1', '0'); grad.setAttribute('x2', '0'); grad.setAttribute('y2', '1');
  const color = total >= 0 ? '#10b981' : '#ef4444';
  [['0%', '0.38'], ['85%', '0.04'], ['100%', '0.0']].forEach(([offset, opacity]) => {
    const stop = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
    stop.setAttribute('offset', offset);
    stop.setAttribute('stop-color', color);
    stop.setAttribute('stop-opacity', opacity);
    grad.appendChild(stop);
  });
  defs.appendChild(grad);
  svg.appendChild(defs);

  let pathD = `M ${coords[0][0]} ${coords[0][1]}`;
  for (let i = 0; i < coords.length - 1; i++) {
    const cp1x = coords[i][0] + (coords[i+1][0] - coords[i][0]) / 2;
    const cp1y = coords[i][1];
    const cp2x = coords[i][0] + (coords[i+1][0] - coords[i][0]) / 2;
    const cp2y = coords[i+1][1];
    pathD += ` C ${cp1x.toFixed(1)} ${cp1y.toFixed(1)}, ${cp2x.toFixed(1)} ${cp2y.toFixed(1)}, ${coords[i+1][0].toFixed(1)} ${coords[i+1][1].toFixed(1)}`;
  }

  const fillD = `${pathD} L ${coords[coords.length-1][0]} ${h-pad} L ${coords[0][0]} ${h-pad} Z`;
  const fillPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  fillPath.setAttribute('d', fillD); fillPath.setAttribute('fill', 'url(#rechartsGrad)');
  svg.appendChild(fillPath);

  const strokePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  strokePath.setAttribute('d', pathD); strokePath.setAttribute('fill', 'none'); strokePath.setAttribute('stroke', color); strokePath.setAttribute('stroke-width', '3.5'); strokePath.setAttribute('stroke-linecap', 'round'); strokePath.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(strokePath);

  const last = coords[coords.length - 1];
  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('cx', last[0]); dot.setAttribute('cy', last[1]); dot.setAttribute('r', '5'); dot.setAttribute('fill', color); dot.setAttribute('stroke', '#fff'); dot.setAttribute('stroke-width', '2');
  svg.appendChild(dot);
  box.appendChild(svg);
}

async function renderHome(){const email=(document.getElementById('customerEmail')?.textContent||'').trim();document.getElementById('homeGreeting').textContent=homeName(email);const av=document.getElementById('homeAvatar');if(av)av.textContent=(email[0]||'A').toUpperCase();try{const p=await api('/api/customer/portfolio');const pnl=(p.positions||[]).reduce((sum,x)=>sum+Number(x.unrealized_pnl||0),0);document.getElementById('homeBalance').textContent=fmtUsdt(p.equity);document.getElementById('homeActive').textContent=String((p.positions||[]).length);const el=document.getElementById('homeOpenPnl');el.textContent=fmtUsdt(pnl,true);el.className=pnlClass(pnl);}catch(e){for(const id of ['homeBalance','homeActive','homeOpenPnl'])document.getElementById(id).textContent='—';}try{const l=await api('/api/customer/ledger');const entries=l.entries||[];const j=groupLedger(entries);renderActivity(j);renderPerformance(j);}catch(e){homeNote(document.getElementById('homeActivity'),'Activity is unavailable right now.');const box=document.getElementById('homeChart');box.replaceChildren();const d=document.createElement('div');d.className='chart-empty';d.textContent='Performance is unavailable right now.';box.appendChild(d);}}
function coinClass(base){return base==='BTC'?'coin-b':base==='ETH'?'coin-e':base==='SOL'?'coin-s':'coin-x';}
function fmtPrice(v){const n=Number(v);if(!isFinite(n))return '—';return n>=1000?n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}):n.toLocaleString('en-US',{maximumFractionDigits:4});}
async function renderWatch(){const ul=document.getElementById('homeWatch');try{const s=await api('/api/customer/market-scanner?exchange=bybit&symbols=BTC%2FUSDT%3AUSDT%2CETH%2FUSDT%3AUSDT%2CSOL%2FUSDT%3AUSDT');const rows=(s.results||[]).filter(x=>x.signal!=='ERROR');if(!rows.length){homeNote(ul,'No market data right now.');return;}ul.replaceChildren();for(const x of rows){const base=String(x.symbol||'').split('/')[0];const li=document.createElement('li');li.className='watch-row';const b=document.createElement('span');b.className='badge '+coinClass(base);b.textContent=base.slice(0,1);const lbl=document.createElement('span');lbl.className='lbl';lbl.textContent=base+' $'+fmtPrice(x.price);const sig=String(x.signal||'NEUTRAL').toUpperCase();const pill=document.createElement('span');pill.className='pill '+(sig==='LONG'?'long':sig==='SHORT'?'short':'neutral');pill.textContent=sig.charAt(0)+sig.slice(1).toLowerCase();li.append(b,lbl,pill);ul.appendChild(li);}}catch(e){homeNote(ul,'Market signals are unavailable right now.');}}
async function executorAction(id,action){try{const d=await api(`/api/customer/executors/${id}/action`,{method:'POST',body:JSON.stringify({action})});document.getElementById('executorOut').textContent=JSON.stringify(d,null,2);await loadExecutors();}catch(e){document.getElementById('executorOut').textContent=e.message;}}

document.addEventListener('click',event=>{
  const el=event.target.closest('[data-customer-action]');
  if(!el)return;
  const action=el.dataset.customerAction;
  const actions={
    'open-settings':()=>document.getElementById('settings').scrollIntoView({behavior:'smooth'}),
    'submit-auth':submitAuth,'toggle-mode':toggleMode,'reset-password':resetPassword,'verify-mfa':verifyMfa,
    'performance-7-days':()=>setPerfTimeframe(7),'performance-30-days':()=>setPerfTimeframe(30),
    'load-product-workspace':loadProductWorkspace,'create-smart-trade':createSmartTrade,'start-bot':startBot,
    'refresh-bot-status':refreshBotStatus,'stop-autonomous-bot':stopAutonomousBot,'connect-oanda':connectOanda,
    'connect-deriv':connectDeriv,'preflight-deriv':preflightDeriv,'create-dca-bot':createDcaBot,'build-strategy':buildStrategy,
    'load-candidates':loadCandidates,'create-lab-candidate':createLabCandidate,'delete-benchmark-captures':deleteBenchmarkCaptures,'refresh-benchmark-capture-status':loadBenchmarkCaptureStatus,'create-paper-executor':()=>createExecutor('PAPER'),
    'request-live-executor':requestLiveExecutor,'load-account':loadAccount,'load-deposit':loadDeposit,'load-withdrawals':loadWithdrawals,
    'send-withdrawal-otp':sendWithdrawalOtp,'verify-withdrawal-otp':verifyWithdrawalOtp,'request-withdrawal':requestWithdrawal,
    'load-billing':loadBilling,'claim-referral':claimReferral,'create-referral-code':createReferralCode,'logout':logout
  };
  const fn=actions[action];
  if(fn){event.preventDefault();fn();}
});
document.addEventListener('change',event=>{
  if(event.target.dataset.customerActionChange==='update-bot-markets')updateBotMarkets();
});
