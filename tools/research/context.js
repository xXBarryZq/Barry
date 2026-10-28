// Research hypotheses only. Timestamps are when information/assessment was available.
export const CONTEXT_POLICY = Object.freeze({
  id:'cn-macro-events-v1',fundingSpreadBp:25,fundingMaxAgeDays:5,pmiMaxAgeDays:45,pmiDecline:.5,
  description:'资金利差连续两期高于25bp，或制造业PMI低于50且环比下降至少0.5，暂停新买入。参数为待验证假设。'
});
const DAY=86_400_000;
const chinaDate=t=>new Date(t+8*3_600_000).toISOString().slice(0,10);
const month=d=>Number(d.slice(0,4))*12+Number(d.slice(5,7));
const unknown=reason=>({known:false,allow:false,reason});
const issueFor=(data,module)=>(data?.issues||[]).some(i=>i.module===module);

export function createContextPolicy(data){
  const valid=data?.schema==='barry-context-v1',cache=new Map();
  const macro=(data?.macro||[]).filter(r=>Number.isFinite(r.availableAt)&&Number.isFinite(r.value));
  const grouped=new Map();
  for(const e of data?.events||[]){const own=grouped.get(e.scope)||[];own.push(e);grouped.set(e.scope,own);}
  const coverage=data?.coverage||[];
  function macroAt(at){
    if(cache.has(at))return cache.get(at);
    const result=calculateMacro(at);cache.set(at,result);return result;
  }
  function calculateMacro(at){
    if(!valid||issueFor(data,'macro'))return unknown('宏观历史未提供或导入校验未通过');
    const latest=indicator=>macro.filter(r=>r.indicator===indicator&&r.availableAt<=at&&r.observationDate<=chinaDate(at))
      .sort((a,b)=>b.observationDate.localeCompare(a.observationDate)).slice(0,2);
    const funds=latest('cn_funding'),pmi=latest('cn_pmi');
    if(funds.length<2||pmi.length<2)return unknown('需要两期已公布的资金面和PMI原始记录');
    const age=r=>(Date.parse(chinaDate(at))-Date.parse(r.observationDate))/DAY;
    if(age(funds[0])>CONTEXT_POLICY.fundingMaxAgeDays||
       (Date.parse(funds[0].observationDate)-Date.parse(funds[1].observationDate))/DAY>7)
      return unknown('资金面资料过期或两期相隔超过7日');
    if(age(pmi[0])>CONTEXT_POLICY.pmiMaxAgeDays||month(pmi[0].observationDate)-month(pmi[1].observationDate)!==1)
      return unknown('PMI资料过期或月份不连续');
    if(funds.some(r=>!Number.isFinite(r.referenceValue)))return unknown('缺少同期7天逆回购操作利率');
    const spreads=funds.map(r=>100*(r.value-r.referenceValue));
    const fundingRisk=spreads.every(v=>v>CONTEXT_POLICY.fundingSpreadBp);
    const pmiRisk=pmi[0].value<50&&pmi[0].value<=pmi[1].value-CONTEXT_POLICY.pmiDecline;
    return {known:true,allow:!fundingRisk&&!pmiRisk,
      reason:[fundingRisk?'资金面偏紧':null,pmiRisk?'PMI偏弱且继续下降':null].filter(Boolean).join('；')||'宏观条件未触发暂停规则',
      fundingSpreadsBp:spreads,pmi:pmi.map(r=>r.value),availableAt:Math.max(...funds.map(r=>r.availableAt),...pmi.map(r=>r.availableAt))};
  }
  function eventsAt(code,at){
    if(!valid||issueFor(data,'events'))return unknown('事件历史未提供或导入校验未通过');
    // General-market coverage never certifies that company news was checked.
    if(!['CN',code].every(scope=>coverage.some(c=>c.scope===scope&&c.startAt<=at&&at<c.endAt)))
      return unknown('缺少该时段的市场或个股事件覆盖声明');
    const active=[...(grouped.get('CN')||[]),...(grouped.get(code)||[])].filter(e=>
      e.availableAt<=at&&e.assessedAt<=at&&e.startAt<=at&&at<e.endAt);
    const risks=active.filter(e=>e.action==='pause_new_entries');
    return {known:true,allow:risks.length===0,reason:risks.length?'重大事件暂停新买入':'已声明覆盖范围内无暂停事件',
      events:active.map(e=>({id:e.id,type:e.type,action:e.action,source:e.source,reference:e.reference,note:e.note}))};
  }
  function at(code,time,variant){
    if(!Number.isFinite(time))return unknown('决策时间无效');
    const states=variant==='macro'?[macroAt(time)]:variant==='events'?[eventsAt(code,time)]:[macroAt(time),eventsAt(code,time)];
    return {known:states.every(s=>s.known),allow:states.every(s=>s.known&&s.allow),reason:states.map(s=>s.reason).join('；')};
  }
  return {at,macroAt,eventsAt,policy:CONTEXT_POLICY};
}
