'use strict';
// Real account data only. Shared monetary billing is not yet enabled.
let workspaceData=null, availableModels=[], products=[], activePage='models', chosenModel=null, usagePage=0;
const workspace=document.createElement('section');workspace.id='workspace';workspace.hidden=true;
$('main').prepend(workspace);
const nav=$('.top nav');
const modelNav=el('button','模型广场','quiet'),keyNav=el('button','Semifly API','quiet');
modelNav.onclick=()=>me?showPage('models'):$('#models').scrollIntoView();keyNav.onclick=()=>showPage('semifly');
nav.querySelectorAll('a').forEach(a=>a.remove());nav.prepend(modelNav,keyNav);
keyNav.hidden=true;
$('#hero h1').textContent='选对模型，清楚付费。';
$('#hero .lede').textContent='五类 API，一个模型广场。直接选择商品，或描述使用场景，查找适合的模型。';
$('#models h2').textContent='五类 API，按需选择。';
$('#models .section-head>p').textContent='场景帮助选模型，不额外收费。未开放的商品不接受付款。';
const menu=document.createElement('details');menu.className='profile-menu';menu.hidden=true;
const summary=el('summary','账户');menu.append(summary);
for(const [name,page] of [['个人信息与安全','account'],['我的密钥','keys'],['用量记录','usage'],['订单与套餐','orders']]){const b=el('button',name,'quiet');b.onclick=()=>{menu.open=false;showPage(page)};menu.append(b)}
const logout=el('button','退出登录','quiet');logout.onclick=()=>$('#sign-out').click();menu.append(logout);nav.append(menu);
function showPage(page){activePage=page;usagePage=0;if(!me){openAuth(false);return}drawWorkspace()}
function action(text,fn,klass=''){const b=el('button',text,klass);b.type='button';b.onclick=fn;return b}
function block(title){const s=el('section',null,'card');s.append(el('h3',title));return s}
const stamp=t=>t?new Date(t*1000).toLocaleString():'—';
function table(headers,rows){const wrap=el('div',null,'table-scroll'),t=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');headers.forEach(h=>hr.append(el('th',h)));head.append(hr);t.append(head);const body=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(value=>{const td=document.createElement('td');td.append(value instanceof Node?value:document.createTextNode(String(value??'—')));tr.append(td)});body.append(tr)});t.append(body);wrap.append(t);return wrap}
renderAccount=function(data){me=data;workspaceData=null;workspace.hidden=!me;menu.hidden=!me;summary.textContent=me?.username||'账户';$('body').classList.toggle('signed-in',!!me);$('#dashboard').hidden=true;$('#hero').hidden=!!me;$('#models').hidden=!!me;$('.principles').hidden=!!me;$('#sign-in').hidden=!!me;$('#get-started').hidden=!!me;$('#hero-start').textContent='Create your account';if(me)refreshDashboard();else workspace.replaceChildren()};
refreshDashboard=async function(){if(!me)return;const userId=me.id;try{const [d,m,p]=await Promise.all([api('/portal/dashboard'),api('/portal/models'),api('/portal/catalog')]);if(!me||me.id!==userId)return;workspaceData=d;availableModels=m;products=p;drawWorkspace()}catch(e){workspace.replaceChildren(el('p',e.message));toast(e.message)}};
async function issueKey(models){if(!models.length){toast('暂无可接入模型');return}const project=workspaceData.projects[0]||await api('/portal/projects',{method:'POST',body:JSON.stringify({name:'Default application'})});const result=await api(`/portal/projects/${project.id}/keys`,{method:'POST',body:JSON.stringify({label:'Semifly application',models})});$('#new-key').textContent=result.key;$('#key-notice').showModal();await refreshDashboard()}
function guarded(fn){return async()=>{try{await fn()}catch(e){toast(e.message)}}}
function drawWorkspace(){if(!workspaceData)return;const d=workspaceData;workspace.replaceChildren();summary.textContent=(me?.username||'账户')+' · 个人中心';modelNav.classList.toggle('selected',activePage==='models');keyNav.classList.toggle('selected',activePage==='semifly');
if(activePage==='models'){
workspace.append(el('p','MODEL MARKETPLACE','eyebrow'),el('h1','五类 API，按需选择。'),el('p','描述场景或查看商品支持的具体型号。当前结算仍为指定模型套餐；分类通用额度尚未开放。','workspace-intro'));
const note=block('账户额度');note.append(el('p','共享美元钱包尚未开放。当前按已购买的指定模型套餐扣除 tokens，不存在模拟余额。'));const balances=el('div',null,'balances');d.balances.forEach(b=>{const item=el('div',null,'balance');item.append(el('small',b.model),el('strong',fmt(b.remaining_tokens)),el('small','tokens remaining'));balances.append(item)});if(!d.balances.length)balances.append(el('p','暂无已购套餐。获取 Key 不代表已获得可消费额度。'));note.append(balances);workspace.append(note);
const market=el('div',null,'market-root');market.id='workspace-market';workspace.append(market);renderMarket(market);
if(chosenModel){const c=block('接入 '+chosenModel);c.id='connection';c.append(el('p','API 地址'),el('code',location.origin+'/v1'),el('p','Model ID'),el('code',chosenModel));const existing=d.api_keys.filter(k=>k.status==='active'&&Array.isArray(k.models)&&k.models.length===1&&k.models[0]===chosenModel);if(existing.length){c.append(el('p','使用已保存的完整专用密钥，仅可调用此型号。'));existing.forEach(k=>c.append(el('p',`${k.label||'应用'} · ${k.prefix}…`)));c.append(action('管理或换发密钥',()=>showPage('keys')))}else c.append(el('p','仅授权此型号；调用时从对应模型套餐扣除额度。生成 Key 不会赠送额度。'),action('生成此模型专用 Key',guarded(()=>issueKey([chosenModel]))));workspace.append(c)}
}else if(activePage==='semifly'){
workspace.append(el('p','SEMIFLY MULTI-MODEL API','eyebrow'),el('h1','一把 Key，按场景选择模型。'),el('p','Semifly 多模型服务：选择使用场景，再确认模型与价格。与模型广场的专用 API 分开购买和计费。'));
const status=block('即将开放 · 暂不收款');status.append(el('p','共享余额、场景模型映射和正式价格尚未配置完成。本页不生成通用 Key、不接受充值，也不会将你的已有套餐自动转换。'));workspace.append(status);
const scenarios=el('div',null,'catalogue');for(const [title,description] of [['编程与开发','代码生成、解释与调试'],['写作与翻译','内容撰写、改写与多语言处理'],['推理与分析','复杂问题、资料分析与归纳']]){const card=block(title);card.append(el('p',description),el('small','支持型号及价格待发布'));scenarios.append(card)}workspace.append(scenarios,el('p','计划接入 DeepSeek、Qwen 等模型；仅在实际接入与计费验证完成后开放。'),action('查看我的现有密钥',()=>showPage('keys')));
}else if(activePage==='usage'){
workspace.append(el('p','个人中心','eyebrow'),el('h1','用量记录'));
const usage=block('最近调用记录');const rows=d.usage.slice(usagePage*10,usagePage*10+10).map(u=>[stamp(u.recorded_at),u.model,u.project,u.prompt_tokens??'—',u.completion_tokens??'—',u.status]);usage.append(table(['时间','模型','应用','输入 tokens','输出 tokens','状态'],rows));if(!rows.length)usage.append(el('p','还没有调用记录。'));usage.append(el('p',`最近 ${d.usage.length} 条 · 第 ${usagePage+1} 页`));const prev=action('上一页',()=>{usagePage--;drawWorkspace()}),next=action('下一页',()=>{usagePage++;drawWorkspace()});prev.disabled=usagePage===0;next.disabled=(usagePage+1)*10>=d.usage.length;usage.append(prev,next);workspace.append(usage);
}else if(activePage==='keys'){
workspace.append(el('p','个人中心','eyebrow'),el('h1','我的密钥'),el('p','统一管理访问凭据。单型号为专用 Key；此前签发的多型号 Key 保留原权限，不代表已开通共享余额。撤销不会清空账户套餐。'),action('去模型广场获取专用 Key',()=>showPage('models')));
const rows=d.api_keys.map(k=>{const controls=el('div');if(k.status==='active'){controls.append(action('撤销',()=>revokeKey(k.id)),action('撤销并换发',guarded(async()=>{if(!confirm('旧 Key 将立即失效，应用需要更新。账户额度和历史保留。继续？'))return;await api(`/portal/keys/${k.id}`,{method:'DELETE'});try{await issueKey(k.models==='*'?availableModels.map(m=>m.id):k.models)}catch(e){await refreshDashboard();throw Error('旧 Key 已撤销，新 Key 未生成，请重新创建：'+e.message)}})))}return[k.label||'应用',k.prefix+'…',k.models==='*'?'全部模型':k.models.join(', '),stamp(k.created_at),k.status==='active'?'有效':'已撤销',controls]});workspace.append(table(['名称','Key','允许模型','创建时间','状态','操作'],rows));if(!rows.length)workspace.append(el('p','还没有密钥。可去模型广场选择模型并获取接入配置。'));
}else if(activePage==='orders'){
workspace.append(el('h1','订单与套餐'),el('p','保留已购买模型套餐及支付记录；不同模型的 tokens 不合并计算。'));workspace.append(table(['订单','模型','购买 tokens','金额','状态','时间'],d.orders.map(o=>[o.code,o.model,fmt(o.token_amount),money({currency:o.currency,price_cents:o.amount_cents}),o.status,stamp(o.created_at)])));if(!d.orders.length)workspace.append(el('p','暂无订单。'));workspace.append(el('p','展示最近 20 笔订单。'));
}else{workspace.append(el('h1','账户与安全'),el('p',`用户名：${me.username}`),el('p',`邮箱：${me.email}`),el('p','Key 遗失或疑似泄露时，撤销并换发。账户额度与历史使用记录保留。'),action('管理密钥',()=>showPage('keys')))}
}
$('#key-notice').addEventListener('close',()=>{$('#new-key').textContent=''});
$('#hero-start').onclick=()=>openAuth(true);
(async()=>{await catalogue();try{renderAccount(await api('/auth/me'))}catch{renderAccount(null)}})();
