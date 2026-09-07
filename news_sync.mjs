// Argus 新闻同步脚本
// 通过 Chrome 调试通道（端口 9223）读取已登录的 Argus 新闻台，
// 把新闻标题/日期/链接同步到 news_cache/news.json，助手直接展示。
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, writeFileSync } from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CDP = "http://127.0.0.1:9223";
const TARGET = "https://direct.argusmedia.com/newsandanalysis";
const CACHE_DIR = join(__dirname, "news_cache");
const CACHE_FILE = join(CACHE_DIR, "news.json");
const PAGE_DUMP = join(CACHE_DIR, "page_debug.html");
const INTERVAL_MS = 10 * 60 * 1000;
const ONCE = process.argv.includes("--once");

mkdirSync(CACHE_DIR, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getPages() {
  const res = await fetch(`${CDP}/json/list`);
  return res.json();
}

async function evalInTab(page, expression, timeoutMs = 30000) {
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let msgId = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++msgId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
  const timeout = setTimeout(() => { try { ws.close(); } catch {} }, timeoutMs);
  try {
    await send("Runtime.enable");
    const result = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    clearTimeout(timeout);
    ws.close();
    if (result.exceptionDetails) {
      throw new Error("page eval exception: " + JSON.stringify(result.exceptionDetails).slice(0, 300));
    }
    return result.result?.value;
  } catch (e) {
    clearTimeout(timeout);
    try { ws.close(); } catch {}
    throw e;
  }
}

async function ensureTab() {
  let pages = await getPages();
  let tab = pages.find((p) => p.type === "page" && p.url.includes("direct.argusmedia.com"));
  if (!tab) {
    const res = await fetch(`${CDP}/json/new?${encodeURIComponent(TARGET)}`, { method: "PUT" });
    tab = await res.json();
  } else if (!tab.url.includes("newsandanalysis")) {
    await evalInTab(tab, `location.href=${JSON.stringify(TARGET)}; "ok"`);
  }
  return tab;
}

const EXTRACT_JS = `(() => {
  const links = Array.from(document.querySelectorAll('a'));
  const items = [];
  const seen = new Set();
  for (const a of links) {
    const t = (a.innerText || '').replace(/\\s+/g, ' ').trim();
    const href = a.href || '';
    if (t.length < 25) continue;
    if (!/news|article|story|analysis|insight/i.test(href)) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    const card = a.closest('article, .card, [class*="news"], [class*="story"]') || a.parentElement || a;
    let date = '';
    const m = (card.innerText || '').match(
      /\\b\\d{1,2}\\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\s+\\d{4}\\b|\\b20\\d{2}-\\d{2}-\\d{2}\\b/i);
    if (m) date = m[0];
    items.push({ title: t.slice(0, 200), href, date });
  }
  const bodyText = (document.body.innerText || '').slice(0, 8000);
  return {
    items: items.slice(0, 60),
    login: /log\\s*in|sign\\s*in/i.test(bodyText),
    title: document.title || '',
  };
})()`;

async function sync() {
  const tab = await ensureTab();
  await sleep(9000); // 等 SPA 渲染
  const data = await evalInTab(tab, EXTRACT_JS);
  if (!data || !Array.isArray(data.items)) {
    throw new Error("提取结果异常");
  }
  // 首次同步保存页面源码，便于后续优化提取规则
  const dump = await evalInTab(tab, "document.documentElement.outerHTML.slice(0, 400000)");
  if (dump) writeFileSync(PAGE_DUMP, dump, "utf-8");
  const payload = {
    synced_at: new Date().toISOString(),
    login: !!data.login,
    page_title: data.title || "",
    items: data.items,
  };
  writeFileSync(CACHE_FILE, JSON.stringify(payload, null, 2), "utf-8");
  console.log(
    `[${new Date().toLocaleString()}] 同步完成：${data.items.length} 条` +
      (data.login ? "（页面显示需要登录）" : "")
  );
}

async function main() {
  // 等待调试端口就绪
  let ready = false;
  for (let i = 0; i < 90; i++) {
    try {
      await getPages();
      ready = true;
      break;
    } catch {
      await sleep(1000);
    }
  }
  if (!ready) {
    console.error("无法连接 Chrome 调试端口 9223，请先运行 启动新闻同步.bat");
    process.exit(1);
  }
  console.log("已连接 Chrome，开始同步 Argus 新闻台 ...");
  while (true) {
    try {
      await sync();
    } catch (e) {
      console.error("同步失败：", e.message);
    }
    if (ONCE) break;
    await sleep(INTERVAL_MS);
  }
}

main();
