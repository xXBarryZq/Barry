export function updatePosition(position,quote,p,market,day,calendar=[]) {
  if(!position||!Number.isFinite(position.entry)||position.entry<=0||!/^\d{4}-\d{2}-\d{2}$/.test(position.date)||!quote||!Number.isFinite(quote.price)||quote.price<=0||day<position.date)return {position,action:null};
  const peak=Math.max(position.entry,position.peak||position.entry,quote.price);
  const next={...position,peak,maxDrawdown:Math.max(position.maxDrawdown||0,1-quote.price/peak),maxLoss:Math.max(position.maxLoss||0,1-quote.price/position.entry)};
  const gain=quote.price/position.entry-1,drawdown=1-quote.price/peak,loss=Math.max(0,-gain);
  const heldDays=[...new Set([...calendar,day])].filter(d=>d>=position.date&&d<=day).length;
  const cause=loss>=p.loss?'相对买入价亏损触及上限':drawdown>=p.drawdown?'从持仓最高价回撤触及上限':gain>=p.target?'达到目标涨幅':heldDays>=p.days?'达到持仓期限':null;
  const locked=market==='cn'&&day===position.date;
  return {position:next,gain,drawdown,loss,locked,heldDays,action:cause?{
    type:locked?'risk':'sell',label:locked?'T+1风险':'卖出',mode:p.id,time:quote.timestamp,barTime:quote.timestamp,
    // Stable through successive quote updates; each reason notifies once per position.
    eventKey:`position:${position.date}:${position.entry}:${cause}`,reason:`${p.label}持仓：${cause}${locked?'；买入当日按T+1锁定，无法按当前价卖出':''}。`,note:'根据手动填写的买入价和已记录最高价计算，未连接实际账户。'
  }:null};
}
