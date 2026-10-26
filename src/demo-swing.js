// Explicitly synthetic, solely for exploring UI; never used by Wind mode.
export function demoSwing(base,now=Date.now(),index=0) {
  const today=new Date(now).toISOString().slice(0,10),dates=[];
  for(let n=300;n>=0;n--){const date=new Date(Date.parse(today)-n*86400000);if(![0,6].includes(date.getUTCDay()))dates.push(date.toISOString().slice(0,10));}
  const dailyBars=[],hourlyBars=[];
  dates.forEach((date,i)=>{
    const close=base*(.80+i*.0009+Math.sin(i/9+index)*.025);
    const time=Date.parse(date+'T00:00:00Z');
    if(date<today)dailyBars.push({date,time,open:close*.998,high:close*1.008,low:close*.992,close,volume:400});
    if(i>80)for(let h=0;h<4;h++){
      const end=time+(6+h)*3600000;
      if(end>now)continue;
      const price=close*(1+(h-1.5)*.001);
      hourlyBars.push({date,time:end,open:price*.999,high:price*1.002,low:price*.998,close:price,volume:100+(i%13===0&&h===3?150:0)});
    }
  });
  return {dailyBars,hourlyBars,calendar:dates,expectedDailyDate:dailyBars.at(-1)?.date,swingRevision:1};
}
