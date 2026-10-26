"""Closed daily and 60-minute Wind histories. Never turn missing data into prices."""
from datetime import datetime, timedelta
from wind_data import cells, finite, timestamp, source_date


def rows(result, kind, now, zone, before=None):
    if getattr(result, 'ErrorCode', None) != 0:
        raise ValueError('Wind 波段历史接口返回错误')
    fields={str(v).upper() for v in (getattr(result,'Fields',None) or [])}
    if not {'OPEN','HIGH','LOW','CLOSE','VOLUME'}.issubset(fields):
        raise ValueError('波段历史缺少 OHLCV')
    bars=[]
    for index,value in enumerate(getattr(result,'Times',None) or []):
        day=source_date(value).isoformat()
        if kind=='daily':
            if before is None or day>=before:
                continue
            end=timestamp(datetime.combine(source_date(value),datetime.min.time()),zone)
        else:
            end=timestamp(value,zone)
            # Wind interval-end convention. Only exact completed 60-minute boundaries.
            # Domestic 10:30/11:30/14:00/15:00 and US :30 endpoints are supported.
            local=datetime.fromtimestamp(end/1000,zone)
            if end>now or local.second or local.minute not in (0,30):
                continue
        row=cells(result,index)
        prices={name:finite(row.get(name.upper()),positive=True) for name in ('open','high','low','close')}
        volume=finite(row.get('VOLUME'))
        good=all(v is not None for v in prices.values()) and volume is not None
        if good:
            good=prices['low']<=min(prices['open'],prices['close']) and prices['high']>=max(prices['open'],prices['close'])
        # Invalid placeholders break signals and make historical evaluations uncertain.
        bars.append({'date':day,'time':end,**prices,'volume':volume,'valid':good})
    return bars


def merge(existing,incoming,key='time',limit=4000):
    values={bar[key]:bar for bar in existing}
    values.update({bar[key]:bar for bar in incoming})
    return sorted(values.values(),key=lambda bar:bar[key])[-limit:]


def fetch_history(api,item,state,zone,now=None):
    clock=datetime.now(zone) if now is None else datetime.fromtimestamp(now/1000,zone)
    today=clock.date().isoformat()
    end=(clock.date()-timedelta(days=1)).isoformat()
    calendar_begin=(clock.date()-timedelta(days=800)).isoformat()
    exchange='NYSE' if item['market']=='us' else 'SSE'
    dates=api.tdays(calendar_begin,end,'TradingCalendar='+exchange)
    if getattr(dates,'ErrorCode',None)!=0:
        raise ValueError('交易日历读取失败')
    calendar=sorted({source_date(value).isoformat() for value in (dates.Data[0] if dates.Data else [])})
    if not calendar:
        raise ValueError('交易日历为空')
    expected=calendar[-1]
    full=not state.get('dailyBars') or state['dailyBars'][-1]['date']!=expected
    begin=(clock.date()-timedelta(days=800 if full else 10)).isoformat()
    daily=api.wsd(item['windCode'],'open,high,low,close,volume',begin,end,'PriceAdj=F')
    daily_bars=merge([] if full else state.get('dailyBars',[]),rows(daily,'daily',int(clock.timestamp()*1000),zone,today),key='date',limit=600)
    by_date={bar['date']:bar for bar in daily_bars}
    daily_bars=[by_date.get(day, {'date':day,'time':timestamp(datetime.fromisoformat(day),zone),'open':None,'high':None,'low':None,'close':None,'volume':None,'valid':False}) for day in calendar if daily_bars and day>=daily_bars[0]['date']]
    hour_begin=(clock-timedelta(days=180 if full or not state.get('hourlyBars') else 3)).replace(tzinfo=None)
    hourly=api.wsi(item['windCode'],'open,high,low,close,volume',hour_begin,clock.replace(tzinfo=None),'BarSize=60;PriceAdj=F')
    hourly_bars=merge([] if full else state.get('hourlyBars',[]),rows(hourly,'hourly',int(clock.timestamp()*1000),zone))
    # Include today's session for horizon accounting, not today's unfinished daily OHLC.
    current_dates={bar['date'] for bar in hourly_bars if bar['date']>=expected}
    calendar=sorted(set(calendar)|current_dates)
    return {'dailyBars':daily_bars,'hourlyBars':hourly_bars,'calendar':calendar,'expectedDailyDate':expected}
