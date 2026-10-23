"""Wind name/code lookup and credential-free local watchlist persistence."""

import csv
import io
import json
import os
import re
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEEDS = json.loads((ROOT / 'src/instruments.json').read_text(encoding='utf-8'))
FUTURES = {'SHF': ('SHFE', {'RB': ('螺纹钢', 1), 'AU': ('沪金', .02)}),
           'DCE': ('DCE', {'I': ('铁矿石', .5)}), 'CZC': ('CZCE', {'MA': ('甲醇', 1)})}


def candidates(query):
    value = query.strip().upper()
    if not re.fullmatch(r'[A-Z0-9.^]{1,24}', value):
        return []
    if value.startswith(('SH.', 'SZ.', 'BJ.')):
        exchange, code = value.split('.', 1)
        value = code + '.' + exchange
    if re.fullmatch(r'\d{6}', value):
        exchange = 'SH' if value[0] in '56' else 'BJ' if value[0] in '489' else 'SZ'
        return [value + '.' + exchange]
    if re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', value):
        return [value]
    if re.fullmatch(r'[A-Z]+\d{0,4}\.(SHF|DCE|CZC)', value):
        return [value]
    if value.startswith('US.'):
        value = value[3:]
    if re.fullmatch(r'[A-Z][A-Z0-9]{0,8}\.(O|N|P|A)', value):
        return [value]
    if re.fullmatch(r'[A-Z][A-Z0-9]{0,8}', value):
        return [value + suffix for suffix in ('.O', '.N', '.P')]
    return []


def instrument(code, name):
    code, name = code.strip().upper(), str(name).strip()
    if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError('证券名称无效')
    if code not in candidates(code):
        raise ValueError('证券代码格式无效')
    symbol, exchange = code.rsplit('.', 1)
    if exchange in ('SH', 'SZ', 'BJ'):
        fund = symbol.startswith(('5', '15', '16'))
        kind = 'ETF' if 'ETF' in name.upper() or symbol.startswith(('51', '56', '58', '159')) else '基金' if fund else '股票'
        item = {'id': exchange + '.' + symbol, 'symbol': symbol, 'name': name,
                'market': 'cn', 'kind': kind, 'currency': 'CNY', 'tick': .001 if fund else .01,
                'rule': 'A 股股票及普通股票 ETF 通常 T+1；卖出标记供已有持仓参考，具体以证券规则为准。'}
    elif exchange in FUTURES:
        market, roots = FUTURES[exchange]
        match = re.fullmatch(r'([A-Z]+)(\d{0,4})', symbol)
        root, contract = match.groups()
        if root not in roots:
            raise ValueError('该期货品种的报价单位尚未配置')
        _, tick = roots[root]
        item = {'id': market + '.' + symbol.lower(), 'symbol': symbol + (' 主连' if not contract else ''),
                'name': name, 'market': 'futures', 'kind': '期货', 'currency': 'CNY', 'tick': tick,
                'continuous': not bool(contract), 'rule': '主连仅供观察；实际交易须核对具体合约、交割日和保证金。'}
    else:
        item = {'id': 'US.' + symbol, 'symbol': symbol, 'name': name, 'market': 'us',
                'kind': 'ETF' if 'ETF' in name.upper() else '股票', 'currency': 'USD', 'tick': .01,
                'rule': '美国证券，美元计价；卖出标记供已有持仓参考。请核对交易权限、汇率及行情时区。'}
    item['windCode'] = code
    return item


class SecurityCatalog:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.a_share_status = '名称目录等待 Wind 加载；可先按完整代码查询。'
        mapping = {'SH.510300':'510300.SH', 'SZ.159915':'159915.SZ', 'SH.600519':'600519.SH',
                   'SZ.300750':'300750.SZ', 'SHFE.rb':'RB.SHF', 'SHFE.au':'AU.SHF',
                   'DCE.i':'I.DCE', 'CZCE.MA':'MA.CZC', 'US.AAPL':'AAPL.O', 'US.NVDA':'NVDA.O',
                   'US.SPY':'SPY.P', 'US.QQQ':'QQQ.O'}
        for seed in SEEDS:
            item = {key:value for key,value in seed.items() if key != 'base'}
            item['windCode'] = mapping[item['id']]
            self.items[item['id']] = item

    def add(self, item):
        with self.lock:
            self.items[item['id']] = dict(item)

    def get(self, value):
        with self.lock:
            if value in self.items:
                return dict(self.items[value])
            codes = candidates(value)
            return next((dict(item) for item in self.items.values() if item['windCode'] in codes), None)

    def search(self, query, limit=15):
        text = query.strip().casefold()
        with self.lock:
            matches = [dict(item) for item in self.items.values() if text and any(text in str(item[key]).casefold() for key in ('name','symbol','id','windCode'))]
        matches.sort(key=lambda item: (not any(text == str(item[key]).casefold() for key in ('name','symbol','id','windCode')), len(item['name']), item['id']))
        return matches[:limit]

    def import_rows(self, rows):
        added = 0
        for row in rows:
            fields = {str(key).strip().casefold(): value for key,value in row.items() if key is not None}
            code = fields.get('wind_code') or fields.get('windcode') or fields.get('证券代码') or fields.get('代码')
            name = fields.get('sec_name') or fields.get('name') or fields.get('证券简称') or fields.get('名称')
            if not code or not name:
                continue
            try:
                self.add(instrument(str(code), name))
                added += 1
            except ValueError:
                continue
        return added

    def import_csv(self, text):
        rows = list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))
        if len(rows) > 20_000:
            raise ValueError('目录最多 20000 行')
        count = self.import_rows(rows)
        if not count:
            raise ValueError('未识别到有效代码和名称列，请使用 wind_code、sec_name 或证券代码、证券简称列名。')
        return count


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.data = {}
        if self.path is not None and self.path.exists():
            if self.path.stat().st_size > 8_000_000:
                raise ValueError('本机配置文件过大，请检查后重试')
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict):
                raise ValueError('本机配置格式无效；程序不会覆盖该文件')

    def save(self, changes):
        updated = {**self.data, **changes}
        encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > 8_000_000:
            raise ValueError('本机名称目录过大，请缩小导入范围。')
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
                    temporary = stream.name
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
        self.data = updated
