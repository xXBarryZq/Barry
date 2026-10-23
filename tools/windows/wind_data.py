"""Read-only Wind data conversion, shared by the local dashboard and tests."""

import math
from datetime import date, datetime, time, timedelta, timezone

CHINA = timezone(timedelta(hours=8))
MINUTE = 60_000
WIND_CODES = {
    'SH.510300': '510300.SH', 'SZ.159915': '159915.SZ',
    'SH.600519': '600519.SH', 'SZ.300750': '300750.SZ',
    'SHFE.rb': 'RB.SHF', 'SHFE.au': 'AU.SHF',
    'DCE.i': 'I.DCE', 'CZCE.MA': 'MA.CZC',
}


def finite(value, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        return None
    return number


def timestamp(value):
    if not isinstance(value, datetime):
        raise ValueError('Wind 时间必须为 datetime')
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA)
    return int(value.timestamp() * 1000)


def source_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.endswith('.0'):
        text = text[:-2]
    for pattern in ('%Y%m%d', '%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    raise ValueError('无法确认源行情日期')


def source_time(value):
    """RT_TIME=153011.0 is HHMMSS, not epoch seconds or a query timestamp."""
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if ':' in text:
        for pattern in ('%H:%M:%S', '%H:%M:%S.%f'):
            try:
                return datetime.strptime(text, pattern).time()
            except ValueError:
                pass
        raise ValueError('源行情时间格式无效')
    number = float(text)
    if not math.isfinite(number) or number < 0:
        raise ValueError('源行情时间无效')
    whole = int(number)
    hour, remaining = divmod(whole, 10_000)
    minute, second = divmod(remaining, 100)
    return time(hour, minute, second)


def cells(result, index=0):
    if getattr(result, 'ErrorCode', None) != 0:
        return {}
    fields = getattr(result, 'Fields', None) or []
    data = getattr(result, 'Data', None) or []
    return {str(field).upper(): data[i][index] for i, field in enumerate(fields)
            if i < len(data) and data[i] is not None and index < len(data[i])}


def quote_from_result(result, date_result, received_at, index=0, date_index=0):
    row = cells(result, index)
    price = finite(row.get('RT_LAST'), positive=True)
    previous = finite(row.get('RT_PRE_CLOSE'), positive=True)
    volume = finite(row.get('RT_VOL'))
    if price is None or previous is None or volume is None:
        raise ValueError('最新价、前收盘或成交量字段缺失')
    issue = ''
    quote_time = None
    label = '—'
    try:
        clock = source_time(row.get('RT_TIME'))
        label = clock.strftime('%H:%M:%S') + ' · 日期待确认'
        # Never derive the source date from result.Times (the request time).
        day = source_date(cells(date_result, date_index).get('RT_DATE'))
        quote_time = timestamp(datetime.combine(day, clock))
        if quote_time > received_at + 10_000:
            quote_time = None
            raise ValueError('源行情时间超前，请核对电脑时间和行情字段')
        label = day.isoformat() + ' ' + clock.strftime('%H:%M:%S')
    except (ValueError, TypeError, OverflowError):
        issue = '报价日期或时间未确认：需核对 Wind RT_DATE / RT_TIME；策略已暂停，查询时间不会代替行情时间。'
    return {
        'price': price, 'previousClose': previous, 'volume': volume,
        'timestamp': quote_time, 'receivedAt': received_at, 'timeLabel': label,
    }, issue


def bars_from_result(result, now):
    """WSI minute timestamps are interval ends; the common schema uses starts."""
    if getattr(result, 'ErrorCode', None) != 0:
        raise ValueError('Wind 分钟接口返回错误')
    times = getattr(result, 'Times', None) or []
    fields = [str(field).upper() for field in (getattr(result, 'Fields', None) or [])]
    required = {'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'}
    if not required.issubset(fields):
        raise ValueError('分钟数据缺少 OHLCV 字段')
    bars, seen, missing = [], set(), 0
    for index, value in enumerate(times):
        try:
            end = timestamp(value)
        except (ValueError, TypeError, OverflowError):
            missing += 1
            continue
        if end % MINUTE or end > now:
            continue
        start = end - MINUTE
        seen.add(start)
        row = cells(result, index)
        prices = {key.lower(): finite(row.get(key), positive=True) for key in required if key != 'VOLUME'}
        volume = finite(row.get('VOLUME'))
        if any(value is None for value in prices.values()) or volume is None:
            missing += 1
            continue
        if prices['low'] > min(prices['open'], prices['close']) or prices['high'] < max(prices['open'], prices['close']):
            missing += 1
            continue
        bars.append({'time': start, **prices, 'volume': volume})
    return bars, seen, missing


def merge_bars(existing, incoming, seen):
    # An explicit NaN correction invalidates an older cached value for that minute.
    merged = {bar['time']: bar for bar in existing if bar['time'] not in seen}
    merged.update({bar['time']: bar for bar in incoming})
    return sorted(merged.values(), key=lambda bar: bar['time'])[-120:]
