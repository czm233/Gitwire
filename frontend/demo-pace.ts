/**
 * 拟人化节奏代理(仅演示运行启用,E2E_DEMO_PACE=1;静默回归不加载本模块)。
 *
 * 框架引擎的浏览器动作没有节奏钩子:Playwright 的 click 是"瞬时落点",
 * 移动与按下背靠背发生,录像里点击永远早于演示光标滑到。为了让
 * 「光标移动 → 到位 → 再点击」成为真实发生的顺序,演示运行时改用
 * connect.cdpEndpoint 接到本模块启动的 Chromium,而非引擎自带启动:
 * 引擎 ↔ 节流代理 ↔ 浏览器。代理只对 Input.*(鼠标/键盘输入)做节流——
 * 同一条串行队列里,输入命令发出前强制等待 PACE_MS,其余命令(导航、
 * 断言求值、截图)原速直通,测试只在"操作"上变慢,等待与断言不变慢。
 * 演示光标(e2e.config.ts 注入)的滑行时长上限小于节流间隔,
 * 恰好在"移动"与"按下"的空档里滑到落点。
 */
import { chromium } from 'playwright-core';
import type { Browser } from 'playwright-core';
import { createServer } from 'node:net';
import { createRequire } from 'node:module';

// 相邻两个输入事件之间的最小间隔;光标滑行上限(e2e.config.ts)必须小于它
const PACE_MS = Number(process.env.E2E_DEMO_PACE_MS ?? 600);

// 本进程启动的演示浏览器,run 结束(reporter.onRunFinished)与进程退出时统一回收
const launched: Array<{ browser: Browser; server: any }> = [];

// 向系统要一个空闲端口:先 listen(0) 占住,读出端口号再释放,把竞态窗口压到最小
const freePort = (): Promise<number> => new Promise((resolve, reject) => {
  const probe = createServer();
  probe.listen(0, '127.0.0.1', () => {
    const port = (probe.address() as { port: number }).port;
    probe.close(() => resolve(port));
  });
  probe.on('error', reject);
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// 输入命令判定:CDP 方法名以 Input. 开头(鼠标移动/按下/抬起/键盘/滚轮/插文本)
const isInputCommand = (raw: string): boolean => {
  try {
    const msg = JSON.parse(raw);
    return typeof msg.method === 'string' && msg.method.startsWith('Input.');
  } catch {
    return false; // 非 JSON 帧按普通命令直通
  }
};

/**
 * 启动一个带节流代理的 Chromium,返回供引擎 connect.cdpEndpoint 使用的 ws 地址。
 * 引擎每个 worker 调一次;返回的每个端点对应一个独立浏览器,互不共享。
 */
export async function pacedCdpEndpoint(): Promise<string> {
  // ws 到演示运行才加载,静默回归连 require 的开销都不付(类型来自 @types/ws)
  const { WebSocketServer, WebSocket } = createRequire(import.meta.url)('ws');
  // 演示浏览器是否有窗口由驾驶舱决定:E2E_DEMO_HEADED=1 时置前可见,默认无头录像
  const headed = process.env.E2E_DEMO_HEADED === '1';
  const debugPort = await freePort();
  const browser = await chromium.launch({
    headless: !headed,
    // remote-debugging 端口与 playwright 自身的 pipe 传输并存,互不干扰
    args: [`--remote-debugging-port=${debugPort}`],
  });
  const version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
  const upstreamUrl: string = version.webSocketDebuggerUrl;

  // 代理监听在另一个本机端口;engine 连它,它连真实浏览器端点
  const proxyPort = await freePort();
  const server: any = new WebSocketServer({ host: '127.0.0.1', port: proxyPort, maxPayload: 0 });
  let retired = false;
  // 回收这套浏览器+代理:引擎断开 5 秒后仍无人连接就执行;引擎重连会重新解析端点、另起新浏览器
  const scheduleRetire = () => {
    const timer = setTimeout(() => {
      if (retired || server.clients.size > 0) return;
      retired = true;
      void browser.close().catch(() => {});
      server.close();
      const at = launched.findIndex((e) => e.server === server);
      if (at >= 0) launched.splice(at, 1);
    }, 5000);
    timer.unref?.();
  };
  server.on('connection', (client: any) => {
    const upstream = new WebSocket(upstreamUrl, { maxPayload: 0 });
    // 上游打开/出错各只通知一次:泵在发送前等它就绪(引擎一连上就会发命令)
    const opened = new Promise<void>((resolve, reject) => {
      upstream.on('open', () => resolve());
      upstream.on('error', reject);
    });
    // 单条先进先出队列保序:输入命令节流、其余命令零延迟,但绝不相换次序
    const queue: string[] = [];
    let draining = false;
    const pump = async () => {
      if (draining) return;
      draining = true;
      while (queue.length && client.readyState === WebSocket.OPEN) {
        const raw = queue.shift() as string;
        if (isInputCommand(raw)) await sleep(PACE_MS); // 输入前强制停顿:拟人节奏的全部秘密
        if (upstream.readyState !== WebSocket.OPEN) {
          try { await opened; } catch { break; } // 上游起不来就放弃这条连接的剩余命令
        }
        upstream.send(raw);
      }
      draining = false;
    };
    client.on('message', (data: unknown) => {
      queue.push(String(data));
      void pump();
    });
    // 浏览器 → 引擎方向(命令响应、事件、录屏帧)永远直通
    upstream.on('message', (data: unknown) => {
      if (client.readyState === WebSocket.OPEN) client.send(String(data));
    });
    const drop = () => {
      try { upstream.close(); } catch { /* 已经断了 */ }
      scheduleRetire();
    };
    client.on('close', drop);
    client.on('error', drop);
  });
  await new Promise<void>((resolve) => server.once('listening', resolve));
  scheduleRetire(); // 引擎一直没连上(解析后被放弃)也回收

  launched.push({ browser, server });
  // 进程退出兜底:正常 close 是异步的,exit 钩子里只能同步杀进程
  process.on('exit', () => { try { browser.process()?.kill(); } catch { /* 已退出 */ } });
  return `ws://127.0.0.1:${proxyPort}`;
}

/** run 结束时由 e2e.config.ts 的 reporter 调用:优雅回收全部演示浏览器。 */
export async function closePacedBrowsers(): Promise<void> {
  await Promise.allSettled(launched.map(async ({ browser, server }) => {
    server.close();
    await browser.close();
  }));
  launched.length = 0;
}
