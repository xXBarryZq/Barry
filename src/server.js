import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve, extname } from 'node:path';
import { timingSafeEqual } from 'node:crypto';
import { Feed, validateUpdate } from './feed.js';

const publicRoot = fileURLToPath(new URL('../public/', import.meta.url));
const files = new Map([['/', 'index.html'], ['/app.js', 'app.js'], ['/style.css', 'style.css'], ['/favicon.svg', 'favicon.svg'], ['/strategy.js', '../src/strategy.js'], ['/swing.js', '../src/swing.js'], ['/positions.js', '../src/positions.js'], ['/alerts.js', 'alerts.js'], ['/market-connection.js', 'market-connection.js']]);
files.set('/context.js','../tools/research/context.js');
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };

export function createMarketServer({ mode = 'demo', token = '', feed = new Feed({ mode }) } = {}) {
  if (!['demo', 'live'].includes(mode)) throw new Error('MARKET_MODE 仅支持 demo 或 live');
  const clients = new Set();
  const send = (response, status, value) => {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify(value));
  };
  const broadcast = () => {
    const event = `data: ${JSON.stringify(feed.snapshot())}\n\n`;
    for (const response of clients) {
      if (response.writableLength > 2_000_000) { response.destroy(); continue; }
      response.write(event);
    }
  };
  const server = http.createServer(async (request, response) => {
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    let path;
    try { path = new URL(request.url, 'http://localhost').pathname; } catch { send(response, 400, { error: '无效地址' }); return; }
    if (request.method === 'GET' && path === '/api/health') {
      const snapshot = feed.snapshot();
      send(response, 200, { status: 'ok', mode, ingestionConfigured: Boolean(token), instruments: snapshot.instruments.length, freshQuotes: snapshot.instruments.filter(item => !item.stale).length });
      return;
    }
    if (request.method === 'GET' && path === '/api/snapshot') { send(response, 200, feed.snapshot()); return; }
    if (request.method === 'GET' && path === '/api/search') { send(response,200,feed.search(new URL(request.url,'http://localhost').searchParams.get('q') || '')); return; }
    if (request.method === 'POST' && ['/api/watchlist','/api/watchlist/remove'].includes(path)) {
      const origin = request.headers.origin;
      if (origin && origin !== `http://${request.headers.host}`) { send(response,403,{error:'仅允许同源页面修改自选。'}); return; }
      if (mode !== 'demo') { send(response,409,{error:'请在 Wind 本机版管理真实行情自选。'}); return; }
      if (!request.headers['content-type']?.startsWith('application/json')) { send(response,415,{error:'仅接受 JSON 请求'}); return; }
      try {
        let body='';
        for await (const chunk of request) { body+=chunk; if(Buffer.byteLength(body)>32768) {send(response,413,{error:'请求过大'});return;} }
        const data=JSON.parse(body);
        if (path.endsWith('/remove')) {
          if(typeof data.id!=='string')throw Error('品种 id 无效');
          feed.remove(data.id); send(response,200,{removed:data.id});
        } else {
          if(typeof data.code!=='string')throw Error('证券代码无效');
          send(response,200,{instrument:feed.add(data.code)});
        }
        broadcast();
      } catch(error) { if(!response.headersSent)send(response,400,{error:error.message}); }
      return;
    }
    if (request.method === 'GET' && path === '/api/events') {
      if (clients.size >= 20) { send(response, 503, { error: '连接数量已达上限' }); return; }
      response.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
      response.write(`retry: 3000\ndata: ${JSON.stringify(feed.snapshot())}\n\n`);
      clients.add(response);
      response.on('close', () => clients.delete(response));
      return;
    }
    if (request.method === 'POST' && path === '/api/ingest') {
      if (mode !== 'live') { send(response, 409, { error: '模拟模式不接受外部行情' }); return; }
      if (!token) { send(response, 503, { error: '尚未配置 MARKET_INGEST_TOKEN' }); return; }
      const received = Buffer.from(request.headers.authorization ?? '');
      const expected = Buffer.from(`Bearer ${token}`);
      if (received.length !== expected.length || !timingSafeEqual(received, expected)) { send(response, 401, { error: '行情写入认证失败' }); return; }
      if (!request.headers['content-type']?.startsWith('application/json')) { send(response, 415, { error: '仅接受 application/json' }); return; }
      try {
        let body = '';
        let bytes = 0;
        for await (const chunk of request) {
          bytes += chunk.length;
          if (bytes > 256_000) { send(response, 413, { error: '请求过大' }); return; }
          body += chunk;
        }
        const payload = JSON.parse(body);
        if (!Array.isArray(payload.quotes) || payload.quotes.length < 1 || payload.quotes.length > 100) throw new Error('quotes 需要包含 1–100 条行情');
        const updates = payload.quotes.map(update => validateUpdate(update));
        const latest = new Map([...feed.states].map(([id, state]) => [id, state.quote?.timestamp ?? 0]));
        for (const update of updates) {
          if (update.timestamp < latest.get(update.id)) throw new Error('拒绝乱序行情');
          latest.set(update.id, update.timestamp);
        }
        updates.forEach(update => feed.apply(update));
        broadcast();
        send(response, 200, { accepted: updates.length });
      } catch (error) { if (!response.headersSent) send(response, 400, { error: error instanceof SyntaxError ? 'JSON 格式错误' : error.message }); }
      return;
    }
    if (request.method === 'GET' && files.has(path)) {
      try {
        const filename = files.get(path);
        const content = await readFile(resolve(publicRoot, filename));
        response.writeHead(200, { 'Content-Type': mime[extname(filename)] });
        response.end(content);
      } catch { send(response, 500, { error: '页面文件不可用' }); }
      return;
    }
    send(response, 404, { error: '接口不存在' });
  });
  const timer = setInterval(() => { feed.tick(); broadcast(); }, 3_000);
  timer.unref();
  server.on('close', () => clearInterval(timer));
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;
  return { server, feed, close: async () => {
    clearInterval(timer);
    clients.forEach(response => response.end());
    await new Promise((done, reject) => server.close(error => error ? reject(error) : done()));
  } };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const mode = process.env.MARKET_MODE || 'demo';
  const port = Number(process.env.PORT || 3000);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT 无效');
  const { server } = createMarketServer({ mode, token: process.env.MARKET_INGEST_TOKEN || '' });
  server.listen(port, process.env.HOST || '127.0.0.1', () => console.log(`Barry 行情看板已启动，端口 ${port}，${mode === 'demo' ? '模拟行情（非真实市场）' : '外部行情模式（等待数据服务）'}`));
  const shutdown = () => { server.close(); server.closeAllConnections(); };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}
