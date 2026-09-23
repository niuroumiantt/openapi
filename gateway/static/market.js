'use strict';
let marketData=null, marketRequest=null;
async function loadMarket(){if(!marketRequest)marketRequest=api('/portal/storefront').then(d=>marketData=d).catch(e=>{marketRequest=null;throw e});return marketRequest}
const intents=[
 {id:'video',label:'视频生成',pattern:/视频|短片|动画|video|movie/i,category:'video'},
 {id:'image',label:'图像生成',pattern:/绘画|图片|图像|海报|画图|image|photo|picture/i,category:'image'},
 {id:'coding',label:'编程开发',pattern:/代码|编程|开发|调试|code|coding|debug/i},
 {id:'writing',label:'翻译写作',pattern:/翻译|写作|文案|论文|文章|translate|writing|translation/i},
 {id:'analysis',label:'推理分析',pattern:/分析|合同|推理|总结|归纳|analysis|reason/i}
];
function renderMarket(target){
 target.replaceChildren();
 if(!marketData){target.append(el('p','模型目录暂时无法加载，请刷新重试。'));return}
 const form=document.createElement('form');form.className='scenario-search';
 const label=el('label','你想用 AI 做什么？');label.htmlFor=target.id+'-scenario';
 const input=document.createElement('input');input.id=label.htmlFor;input.maxLength=300;input.placeholder='例如：翻译英文论文、给商品生成图片、制作短视频';
 const submit=el('button','推荐模型');submit.type='submit';form.append(label,input,submit);
 const results=el('section');results.setAttribute('aria-live','polite');
 const grid=el('div',null,'catalogue'),detail=el('section',null,'card');detail.hidden=true;
 const showModel=m=>{detail.hidden=false;detail.replaceChildren(el('h3',m.id),el('p','已配置路由；场景适配和输出质量请以实际测试为准。'));
   if(!m.offers.length)detail.append(el('p','尚未发布价格与套餐，暂不接受付款。'));
   m.offers.forEach(p=>{detail.append(el('p',`${fmt(p.token_amount)} tokens · ${money(p)} · 指定型号套餐`),action('购买此套餐',()=>checkout(p.id)))});
   if(me)detail.append(action('查看接入配置',()=>{chosenModel=m.id;showPage('models');$('#connection')?.scrollIntoView({block:'center'})}));
   else detail.append(action('登录管理接入',()=>openAuth(false)));
 };
 for(const category of marketData.categories){const members=marketData.models.filter(m=>m.category===category.id);const card=block(category.name);card.append(el('p',category.description),el('small',members.length?`${members.length} 个已配置型号 · 价格以明细为准`:'即将开放 · 暂不收款'),action('查看支持模型',()=>{detail.hidden=false;detail.replaceChildren(el('h3',category.name));if(!members.length)detail.append(el('p','尚无已审核并配置的型号。不会展示虚构模型或价格。'));members.forEach(m=>detail.append(action(m.id,()=>showModel(m))));}));grid.append(card)}
 const recommend=(intent)=>{results.replaceChildren(el('h3',intent.label+' · 场景匹配'));
 const candidates=marketData.models.filter(m=>intent.category?m.category===intent.category:m.scenarios.includes(intent.id)).slice(0,3);
 if(!candidates.length){results.append(el('p','这个场景暂时没有已配置的推荐模型，请查看对应商品的开放状态。'));return}
 results.append(el('p','按已维护的场景标签匹配，不是模型能力排名；同一型号与商品列表使用同一价格。'));
 candidates.forEach(m=>{const card=block(m.id);card.append(el('p',`匹配原因：已标注支持「${intent.label}」场景。`),el('p',marketData.categories.find(c=>c.id===m.category).name),el('p',m.offers.length?m.offers.map(p=>`${fmt(p.token_amount)} tokens / ${money(p)}`).join('；'):'价格待发布 · 暂不可购买'),action('查看详情',()=>showModel(m)));results.append(card)});
 };
 form.onsubmit=e=>{e.preventDefault();detail.hidden=true;const text=input.value.trim();results.replaceChildren();if(!text){results.append(el('p','请先描述用途，或输入具体模型名称。'));return}
 const exact=marketData.models.find(m=>m.id.toLowerCase()===text.toLowerCase());if(exact){showModel(exact);return}
 const matches=intents.filter(i=>i.pattern.test(text));if(matches.length===1){recommend(matches[0]);return}
 results.append(el('p',matches.length?'你更希望先完成哪类任务？':'请再选一个主要用途，帮助我们缩小范围：'));(matches.length?matches:intents).forEach(i=>results.append(action(i.label,()=>recommend(i))));};
 target.append(form,results,grid,detail);
}
catalogue=async function(){const root=$('#catalogue');root.className='market-root';try{await loadMarket();renderMarket(root)}catch(e){root.replaceChildren(el('p','目录加载失败，请刷新重试。'))}};
