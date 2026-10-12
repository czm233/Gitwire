#!/usr/bin/env python3
"""回归驾驶舱:浏览器打开 http://127.0.0.1:10247,实时看 e2e 案例表、当前进度、控制台输出,
并可随时停止/重跑/只跑失败项。测试事件由 frontend/e2e.config.ts 的 dashboard reporter 实时推送。

端口 10247 已登记(e2e_dashboard);隔离栈(10248/10249)由本脚本按需拉起、退出时收摊。
"""
from __future__ import annotations

import atexit
import faulthandler
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

faulthandler.register(signal.SIGUSR1, all_threads=True)  # kill -USR1 <pid> 可随时 dump 线程栈

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
PORT = 10247
SELF = f'http://127.0.0.1:{PORT}'
LIVE_FRAME = ROOT / 'data' / 'e2e-stack' / 'live.jpg'  # 与 e2e 栈同目录,由本脚本管理
HISTORY = ROOT / 'data' / 'test-history.jsonl'  # 每轮运行摘要,趋势面板数据源


def append_history(entry: dict) -> None:
    """追加一轮运行摘要(全量/子集分别标记,趋势统计只用全量轮)。"""
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open('a') as file:
            file.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass  # 历史写失败不影响本轮运行结果

lock = threading.RLock()
events_queue: "queue.Queue[dict]" = queue.Queue()
stack_owned = [False]
runner_proc: list[subprocess.Popen | None] = [None]
stop_requested = [False]

def scan_cases() -> list[dict]:
    """静态扫描测试声明;测试体内出现 agent.* 的标记为 AI 驱动,其余为确定性断言。"""
    cases: list[dict] = []
    declaration = re.compile(r"(?<!\.)\btest\.setup\(\s*'([^']+)'|(?<!\.)\btest\(\s*'([^']+)'\s*(?:,\s*\{|,)")
    for path in sorted((FRONTEND / 'e2e').glob('*.e2e.ts')):
        text = path.read_text()
        matches = list(declaration.finditer(text))
        for index, match in enumerate(matches):
            title = match.group(1) or match.group(2)
            body = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
            cases.append({'title': title, 'file': path.name,
                          'kind': 'setup' if match.group(1) else 'test',
                          'ai': bool(re.search(r'\bagent\.', body)),
                          'smoke': bool(re.search(r"tags:\s*\[[^\]]*['\"]smoke['\"]", body)),
                          'code': body.strip()[:3000],
                          'state': 'pending', 'durationMs': None, 'note': '', 'info': ''})
    return cases

def new_state() -> dict:
    return {'status': 'idle', 'total': None, 'headed': False, 'startedAt': None, 'finishedAt': None,
            'cases': scan_cases(), 'log': deque(maxlen=200), 'steps': deque(maxlen=40)}

state = new_state()

def log(line: str) -> None:
    if not line.strip():
        return
    stamp = time.strftime('%H:%M:%S')
    with lock:
        state['log'].append(f'{stamp}  {line}')

def dig(obj, key):
    """在嵌套事件 JSON 里找第一个叫 key 的字段(test-finished 的结果结构可能嵌在 result/test 下)。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = dig(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = dig(item, key)
            if found is not None:
                return found
    return None

def find_artifact(obj, suffix: str) -> str:
    """在结果记录里递归找指定后缀的工件路径(相对 .e2e/results)。"""
    if isinstance(obj, str):
        return obj if obj.endswith(suffix) else ''
    if isinstance(obj, dict):
        for value in obj.values():
            hit = find_artifact(value, suffix)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = find_artifact(item, suffix)
            if hit:
                return hit
    return ''


def find_video(obj) -> str:
    """在结果记录里递归找 .webm/.mp4 工件路径(相对 .e2e 输出目录)。"""
    if isinstance(obj, str):
        return obj if obj.endswith(('.webm', '.mp4')) else ''
    if isinstance(obj, dict):
        for value in obj.values():
            hit = find_video(value)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = find_video(item)
            if hit:
                return hit
    return ''


def find_case(title: str) -> dict | None:
    for case in state['cases']:
        if case['title'] == title:
            return case
    return None

def apply_event(event: dict) -> None:
    kind = event.get('type')
    with lock:
        if kind == 'plan':
            state['total'] = state['total'] or dig(event, 'total') or len(state['cases'])
        elif kind == 'test-started':
            title = dig(event, 'title') or ''
            case = find_case(title)
            if case is None:
                case = {'title': title, 'file': '', 'kind': 'test', 'ai': False, 'smoke': False, 'state': 'pending', 'durationMs': None, 'note': '', 'info': ''}
                state['cases'].append(case)
            case['state'] = 'running'
            case['note'] = ''
            case['log'] = []  # 新一轮开始,清空本条的步骤日志
        elif kind == 'step':
            # 每个步骤的 end 阶段:归属到对应用例,追加实时日志行
            progress = event.get('progress') or {}
            if progress.get('phase') == 'end':
                test_id = str(event.get('testId') or '')
                title = urllib.parse.unquote(test_id.split('::')[-1]) if '::' in test_id else ''
                case = find_case(title)
                if case is not None:
                    status = progress.get('status', 'passed')
                    mark = {'passed': '✓', 'failed': '×', 'timed-out': '×'}.get(status, '·')
                    label = progress.get('label') or progress.get('api') or 'step'
                    duration = progress.get('durationMs')
                    line = f"{mark} {label}" + (f" ({duration}ms)" if duration is not None else '')
                    case['log'] = (case.get('log') or [])[-79:] + [line]
                    if status in ('failed', 'timed-out') and progress.get('error'):
                        case['log'] = case['log'][-79:] + [f"  错误:{str(progress['error'])[:200]}"]
        elif kind == 'setup':
            title = dig(event, 'title') or ''
            case = find_case(title)
            if case and dig(event, 'state') == 'finished':
                case['state'] = 'passed'
        elif kind == 'test-finished':
            title = dig(event, 'title') or ''
            case = find_case(title)
            if case is None:
                case = {'title': title, 'file': '', 'kind': 'test', 'ai': False, 'smoke': False, 'state': 'pending', 'durationMs': None, 'note': '', 'info': ''}
                state['cases'].append(case)
            raw = dig(event, 'status') or 'failed'
            case['state'] = {'passed': 'passed', 'failed': 'failed', 'timed-out': 'failed',
                             'blocked': 'cancelled', 'cancelled': 'cancelled', 'skipped': 'skipped'}.get(raw, 'failed')
            case['durationMs'] = dig(event, 'durationMs')
            error = dig(event, 'error') or ''
            explanation = dig(event, 'explanation') or ''
            if error:
                case['note'] = str(error)[:300]
            if explanation:
                case['info'] = str(explanation)[:300]
            video = find_artifact(event, '.webm') or find_artifact(event, '.mp4')
            if video:
                case['video'] = video
            test_id = (event.get('result', {}).get('test') or {}).get('id')
            if test_id:
                case['testId'] = test_id
        elif kind == 'output':
            text = dig(event, 'text') or dig(event, 'chunk') or dig(event, 'data') or ''
            if text:
                state['log'].append(str(text).rstrip()[:400])
        elif kind == 'traces':
            for title, path in (event.get('traces') or {}).items():
                case = find_case(title)
                if case:
                    case['trace'] = str(path).split('results/', 1)[-1]
        elif kind in ('run-finished', 'run-stopped', 'run-interrupted', 'run-error'):
            for test_id, path in (event.get('traces') or {}).items():
                case = next((c for c in state['cases'] if c.get('testId') == test_id), None)
                if case:
                    case['trace'] = str(path).split('results/', 1)[-1]
            state['finishedAt'] = time.time()
            new_status = {'run-finished': 'finished', 'run-stopped': 'stopped',
                          'run-interrupted': 'stopped', 'run-error': 'error'}[kind]
            # 中断后的收尾 run-finished 不把"已停止"降级成"已完成"
            if not (new_status == 'finished' and state['status'] == 'stopped'):
                state['status'] = new_status
            for case in state['cases']:
                if case['state'] == 'running':
                    case['state'] = 'cancelled'

def watch_frontmost(stop) -> None:
    """macOS 后台进程开的窗口不会自动置前;headed 运行时每秒把测试浏览器推到最前。"""
    while not stop.is_set():
        subprocess.run(['osascript', '-e',
                        'tell application "System Events" to set frontmost of '
                        '(first application process whose name contains "Chrome for Testing") to true'],
                       capture_output=True)
        stop.wait(1.2)


def watch_live(stop) -> None:
    """headed 运行期间每 0.8s 抓一帧屏幕作为驾驶舱直播画面。"""
    LIVE_FRAME.parent.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        try:
            result = subprocess.run(['screencapture', '-x', '-t', 'jpg', str(LIVE_FRAME)],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                log(f'直播抓帧失败 rc={result.returncode}: {result.stderr[:160]}')
        except Exception as error:  # noqa: BLE001
            log(f'直播抓帧异常: {error}')
        stop.wait(0.8)


def start_run(mode: str, selected: list[str] | None = None, headed: bool = False, video: bool = False) -> None:
    def worker() -> None:
        t0 = time.time()  # 本轮起始时间(局部):落盘用,避免读到下一轮重置后的 state
        with lock:
            state['cases'] = scan_cases()  # 每轮重扫:新写的测试文件自动出现在案例表
            for case in state['cases']:
                case.update(state='pending', durationMs=None, note='', info='')
            state['status'] = 'starting'
            state['headed'] = headed
            state['total'] = len(selected) if selected else None
            state['startedAt'] = time.time()
            state['finishedAt'] = None
        log('拉起 e2e 隔离栈(10248/10249)…')
        up = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'up'],
                            cwd=ROOT, capture_output=True, text=True)
        if up.returncode:
            with lock:
                state['status'] = 'error'
            log('隔离栈启动失败:' + (up.stdout + up.stderr).strip()[-300:])
            return
        stack_owned[0] = True
        extras = []
        if selected:
            pattern = '^(?:' + '|'.join(re.escape(t) for t in selected) + ')$'
            extras += ['--grep', pattern]
            log(f'只运行选中的 {len(selected)} 条(所需前置会自动附带)…')
        elif mode == 'last-failed':
            extras += ['--last-failed']
            log('只跑上次失败项…')
        else:
            log('开始运行全部用例…')
        if headed:
            extras += ['--headed']
            log('可视化运行:浏览器置前 + 驾驶舱直播面板,串行单窗口便于跟踪…')
        if video:
            extras += ['--video=on']
            log('录制回放视频:每条用例一个 webm,跑完可在案例行点「观看回放」…')
        # --trace:每条用例都留步骤记录(官方默认只写失败的),供「步骤」按钮查看
        extras += ['--trace']
        args = ['npm', 'run', 'e2e'] + (['--', *extras] if extras else [])
        run_env = {**os.environ, 'E2E_DASHBOARD': f'{SELF}/api/event'}
        if headed:
            run_env['E2E_WORKERS'] = '1'  # 串行:一次一个窗口,直播画面才跟得上
        proc = subprocess.Popen(args, cwd=FRONTEND, env=run_env,
                                start_new_session=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        runner_proc[0] = proc
        stop_watch = threading.Event()
        watchers = []
        if headed and sys.platform == 'darwin':
            watchers.append(threading.Thread(target=watch_frontmost, args=(stop_watch,), daemon=True))
            watchers.append(threading.Thread(target=watch_live, args=(stop_watch,), daemon=True))
            for w in watchers:
                w.start()
        try:
            for line in proc.stdout or []:
                log(line.rstrip())
            proc.wait()
        finally:
            stop_watch.set()
            for w in watchers:
                w.join(timeout=2)
        runner_proc[0] = None
        # 先在锁内对案例做快照:下一轮此刻可能已 rescan 重置共享 state
        with lock:
            if state['status'] in ('running', 'starting'):
                state['status'] = 'finished' if proc.returncode == 0 else 'stopped' if stop_requested[0] else 'error'
                state['finishedAt'] = time.time()
            snapshot = [dict(c) for c in state['cases']]
            final_status = state['status']
        counts = {s: sum(1 for c in snapshot if c['state'] == s) for s in ('passed', 'failed')}
        log(f"运行结束:通过 {counts['passed']} · 失败 {counts['failed']}(退出码 {proc.returncode})")
        # 历史落盘:本轮摘要(局部时间+快照统计,与共享 state 解耦)
        append_history({
            'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'kind': 'subset' if selected else ('last-failed' if mode == 'last-failed' else 'full'),
            'status': final_status,
            'passed': counts['passed'], 'failed': counts['failed'],
            'total': len(selected) if selected else len(snapshot),
            'duration_s': round(time.time() - t0, 1),
            'slow_cases': [
                {'title': c['title'], 's': round(c['durationMs'] / 1000, 1)}
                for c in snapshot
                if (c.get('durationMs') or 0) > 3000
            ],
        })
        stop_requested[0] = False

    threading.Thread(target=worker, daemon=True).start()

def stop_run() -> None:
    proc = runner_proc[0]
    if not proc:
        return
    stop_requested[0] = True
    log('收到停止指令,正在收尾(等待运行器优雅退出)…')
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass

def teardown() -> None:
    if runner_proc[0]:
        stop_run()
        for _ in range(50):
            if runner_proc[0] is None:
                break
            time.sleep(0.2)
    if stack_owned[0]:
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'down'], cwd=ROOT,
                       capture_output=True)
        stack_owned[0] = False

PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Gitwire 回归驾驶舱</title><style>
  :root{--bg:#0b1622;--panel:#0f1e2e;--ink:#e8f0f6;--dim:#8fa5b8;--green:#7ef29a;--red:#ff7b72;--amber:#e3b341;--line:#1d3247}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.6 ui-monospace,"SF Mono",Menlo,monospace;padding:20px}
  .wrap{max-width:980px;margin:0 auto}
  h1{font-size:17px;margin:0;font-weight:600;letter-spacing:.5px}
  header{display:flex;align-items:center;gap:10px;user-select:none;flex-wrap:wrap}
  .pill{padding:2px 10px;border:1px solid var(--line);border-radius:99px;font-size:12px;color:var(--dim)}
  .pill.run{color:var(--green);border-color:var(--green)} .pill.bad{color:var(--red);border-color:var(--red)}
  button{background:none;border:1px solid var(--line);color:var(--ink);border-radius:6px;
    padding:5px 12px;font:inherit;cursor:pointer}button:hover{border-color:var(--green);color:var(--green)}
  button:disabled{opacity:.4;cursor:default}button.danger:hover{border-color:var(--red);color:var(--red)}
  .bar{height:6px;background:var(--panel);border-radius:3px;margin:14px 0 4px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--green);width:0;transition:width .4s}
  .bar>i.bad{background:var(--red)}
  .meta{color:var(--dim);font-size:12px}
  table{width:100%;border-collapse:collapse;margin-top:14px}th,td{text-align:left;padding:7px 10px;
    border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink);font-weight:600}
  td.dim,th.dim{color:var(--dim)}td.num{color:var(--dim);white-space:nowrap}
  .st{white-space:nowrap}.st.passed{color:var(--green)}.st.failed{color:var(--red)}
  .st.running{color:var(--amber);animation:pulse 1s infinite}.st.cancelled,.st.pending,.st.skipped{color:var(--dim)}
  .tag{display:inline-block;margin-left:6px;padding:0 6px;border-radius:99px;font-size:11px;vertical-align:1px;user-select:none}
  .tag.ai{color:var(--amber);border:1px solid var(--amber)} .tag.det{color:var(--dim);border:1px solid var(--line)}
  .fbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--dim);user-select:none}
  .fbtn{font-size:12px;padding:2px 10px;border-radius:99px}
  .fbtn.on{color:var(--amber);border-color:var(--amber)}
  @keyframes pulse{50%{opacity:.4}}
  .note{color:var(--red);font-size:12px;white-space:pre-wrap}
  .info{color:var(--dim);font-size:12px;white-space:pre-wrap}
  .watch{font-size:11px;padding:1px 8px;margin-left:6px;color:var(--green);border-color:var(--line)}
  .watch.src,.watch.trc{color:var(--dim)}
  .detail-pre{max-height:460px;overflow:auto;font-size:12px;line-height:1.55;color:var(--ink);
    white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0}
  .player-box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
  .player-box video{width:100%;border-radius:6px;background:#000}
  tr.player-row>td{padding:6px 10px 14px;border-bottom:1px solid var(--line)}
  #live{display:none;margin-top:14px;background:var(--panel);border:1px solid var(--amber);border-radius:8px;padding:10px}
  #live img{width:100%;border-radius:6px;background:#000}
  #speed{width:180px;accent-color:var(--amber)}
  #history{display:none;margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font-size:12px}
  #history table{margin-top:6px}#history td,#history th{padding:3px 8px}
  .console{margin-top:8px;background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:10px 12px;height:190px;overflow:auto;font-size:12px;color:var(--dim);white-space:pre-wrap}
  .console b{color:var(--green);font-weight:400}
  #b-log{font-size:12px;color:var(--dim);padding:3px 10px;margin-top:14px}
  #b-log:hover{color:var(--ink);border-color:var(--line)}
</style></head><body><div class="wrap">
<header><h1>Gitwire 回归驾驶舱</h1><span class="pill" id="pill">—</span><span class="meta" style="font-size:11px">关闭会话即自动收摊</span>
  <span style="flex:1"></span>
  <button id="b-start">▶ 全量回归</button>
  <button id="b-selected" disabled>▶ 运行选中</button>
  <label style="display:flex;align-items:center;gap:5px;user-select:none;font-size:12px;color:var(--dim)"><input type="checkbox" id="c-headed">显示浏览器窗口</label>
  <label style="display:flex;align-items:center;gap:5px;user-select:none;font-size:12px;color:var(--dim)"><input type="checkbox" id="c-video">录制回放视频</label>
  <button id="b-stop" class="danger" disabled>■ 停止</button>
  <button id="b-failed" disabled>↻ 只跑失败项</button>
</header>
<div class="bar"><i id="fill"></i></div>
<div class="meta" id="meta">待命</div>
<div class="fbar">类型
  <button class="fbtn on" data-tf="all">全部</button><button class="fbtn" data-tf="det">确定性</button><button class="fbtn" data-tf="ai">AI·模型</button><button class="fbtn" data-tf="setup">前置</button>
  <span style="width:12px"></span>
  <button class="fbtn" id="f-smoke">仅冒烟</button>
  <span style="flex:1"></span><span id="f-count"></span></div>
<table><thead><tr><th class="dim">选</th><th class="dim">#</th><th>测试案例</th><th>类型</th><th class="dim">冒烟</th><th>状态</th><th class="dim">耗时</th></tr></thead>
<tbody id="rows"></tbody></table>
<button id="b-log">▸ 运行日志</button>
  <button id="b-history">📈 趋势</button>
<div id="live"><div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
  <span style="color:var(--amber)">● LIVE</span>
  <strong id="live-title" style="font-size:13px"></strong></div>
  <img id="live-img" alt="测试浏览器实时画面"></div>
<div id="history"></div>
<div class="console" id="console" hidden></div>
</div><script>
const $ = id => document.getElementById(id);
const STAT = {pending:'等待',running:'▶ 运行中',passed:'✓ 通过',failed:'× 失败',cancelled:'— 取消',skipped:'⏭ 跳过'};
let busy = false, showLog = false, userToggled = false, lastStatus = '';
let typeFilter = 'all', smokeOnly = false;
const typeMatch = { all: () => true, det: c => !c.ai && c.kind !== 'setup', ai: c => !!c.ai, setup: c => c.kind === 'setup' };
const selection = new Set();
  // 行内回放:播放器展开在所属用例行的正下方
  const playerHost = document.createElement('tr');
  playerHost.className = 'player-row';
  playerHost.innerHTML = '<td colspan="5"><div class="player-box"><div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap"><strong id="player-title" style="font-size:13px"></strong><input type="range" id="speed" min="0.1" max="3" step="0.05" value="1"><span id="speed-label" style="color:var(--amber);min-width:38px">1×</span><button data-rate="0.25">0.25×</button><button data-rate="0.5">0.5×</button><button data-rate="1">1×</button><span style="flex:1"></span><button id="player-close" class="danger">收起回放</button></div><video id="player-video" controls></video></div></td>';
  const playerState = { title: '' };
  const detailState = { title: '', mode: '' };
  const detailHost = document.createElement('tr');
  detailHost.className = 'player-row';
  detailHost.innerHTML = '<td colspan="5"><div class="player-box" style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap"><strong id="detail-title" style="font-size:13px"></strong><span id="detail-mode" class="tag det"></span><span style="flex:1"></span><button id="detail-close" class="danger">收起</button></div><pre class="detail-pre" id="detail-pre">加载中…</pre></td>';
  const closeDetail = () => { detailState.title=''; detailState.mode=''; detailHost.remove(); };
  detailHost.querySelector('#detail-close').onclick = closeDetail;
  const openDetail = (row, title, mode, fill) => {
    closePlayer(); closeDetail();
    detailState.title = title; detailState.mode = mode;
    row.after(detailHost);
    document.getElementById('detail-title').textContent = title;
    document.getElementById('detail-mode').textContent = { code: '源码·判定怎么写', trace: '步骤·这轮怎么跑的', log: '日志·本条实时输出' }[mode] || mode;
    const pre = document.getElementById('detail-pre');
    pre.textContent = '加载中…';
    Promise.resolve(fill()).then(t => { pre.textContent = t || '(无内容)'; }).catch(e => { pre.textContent = '加载失败:' + e; });
    detailHost.scrollIntoView({behavior:'smooth', block:'center'});
  };
  let lastCases = {};
  const closePlayer = () => { const v=document.getElementById('player-video'); if (v) { v.pause(); v.removeAttribute('src'); } playerState.title=''; playerHost.remove(); };
  playerHost.querySelector('#player-close').onclick = closePlayer;
  playerHost.querySelectorAll('[data-rate]').forEach(b => b.onclick = () => {
    const r = +b.dataset.rate;
    playerHost.querySelector('#speed').value = r;
    playerHost.querySelector('#speed-label').textContent = r + '×';
    document.getElementById('player-video').playbackRate = r;
  });
  playerHost.querySelector('#speed').oninput = e => {
    const r = +e.target.value;
    playerHost.querySelector('#speed-label').textContent = r + '×';
    document.getElementById('player-video').playbackRate = r;
  };
const ctl = (a, extra) => fetch('/api/control', {method:'POST', headers:{'content-type':'application/json'},
  body: JSON.stringify({action:a, ...extra})});
function tick(){fetch('/events').then(r=>r.json()).then(s=>{
  busy = ['starting','running'].includes(s.status);
  const pill=$('pill');
  pill.className='pill '+((s.status==='running'||s.status==='starting')?'run':(s.status==='error'||s.status==='stopped'||s.cases.some(c=>c.state==='failed'))?'bad':'');
  pill.textContent={idle:'待命',starting:'准备中',running:'运行中',finished:'已完成',stopped:'已停止',error:'出错'}[s.status]||s.status;
  const cases=s.cases;
  const p=cases.filter(c=>c.state==='passed').length, f=cases.filter(c=>c.state==='failed').length;
  const skipped=cases.filter(c=>c.state==='skipped').length;
  const total=s.total||cases.length;
  $('fill').style.width=(total?(p+f)/total*100:0)+'%';
  $('fill').className=f?'bad':'';
  const ai = s.cases.filter(c=>c.ai).length;
  $('meta').textContent=`共 ${total} 条(确定性 ${total-ai} · AI ${ai}) · 本轮 ✓${p} ×${f}${skipped?` ⏭${skipped}`:''}` + (s.startedAt?` · 开始于 ${new Date(s.startedAt*1000).toLocaleTimeString()}`:'');
  for (const c of cases) lastCases[c.title] = c;
  const fullRender = () => {
  const visible = cases.filter(c => typeMatch[typeFilter](c) && (!smokeOnly || c.smoke));
  $('f-count').textContent = `${visible.length}/${cases.length} 条`;
  document.querySelectorAll('.fbtn[data-tf]').forEach(b => b.classList.toggle('on', b.dataset.tf === typeFilter));
  $('f-smoke').classList.toggle('on', smokeOnly);
  $('rows').innerHTML=visible.map((c,i)=>`<tr data-title="${c.title.replace(/"/g,'&quot;')}"><td><input type="checkbox" data-t="${c.title.replace(/"/g,'&quot;')}" ${selection.has(c.title)?'checked':''}></td><td class="num">${i+1}</td>
    <td>${c.title}${c.note?`<div class="note">${c.note}</div>`:''}${c.info?`<div class="info">${c.info}</div>`:''}${c.video?`<button class="watch" data-video="${encodeURIComponent(c.video)}" data-title="${c.title.replace(/"/g,'&quot;')}">▶ 观看回放</button>`:''}<button class="watch src" data-mode="code" data-title="${c.title.replace(/"/g,'&quot;')}">源码</button>${c.trace?`<button class="watch trc" data-mode="trace" data-title="${c.title.replace(/"/g,'&quot;')}">步骤</button>`:''}${c.log&&c.log.length?`<button class="watch trc" data-mode="log" data-title="${c.title.replace(/"/g,'&quot;')}">日志</button>`:''}</td>
    <td class="nw">${c.kind==='setup'?'<span class="tag det">前置</span>':c.ai?'<span class="tag ai">AI·模型</span>':'<span class="tag det">确定性</span>'}</td>
    <td class="nw" style="color:${c.smoke?'var(--green)':'var(--dim)'}">${c.smoke?'●':'—'}</td>
    <td class="st ${c.state}">${STAT[c.state]||c.state}</td>
    <td class="num">${c.durationMs!=null?(c.durationMs/1000).toFixed(1)+'s':''}</td></tr>`).join('');
  };
  if (playerState.title || detailState.title) {
    // 展开面板期间只更新状态单元格,避免整表重绘把面板冲掉
    for (const c of cases) {
      const tr = document.querySelector('#rows tr[data-title="'+CSS.escape(c.title)+'"]');
      if (!tr) continue;
      const st = tr.querySelector('.st');
      if (st) { st.className = 'st ' + c.state; st.textContent = STAT[c.state] || c.state; }
    }
  } else {
    fullRender();
  }
  document.querySelectorAll('#rows .watch').forEach(b => b.onclick = () => {
    if (b.dataset.mode) {  // 源码 / 步骤 详情
      const case_ = lastCases[b.dataset.title] || {};
      if (detailState.title === b.dataset.title && detailState.mode === b.dataset.mode) { closeDetail(); return; }
      if (b.dataset.mode === 'code') {
        openDetail(b.closest('tr'), b.dataset.title, 'code', async () => case_.code || '(无源码)');
      } else if (b.dataset.mode === 'log') {
        openDetail(b.closest('tr'), b.dataset.title, 'log', async () => (case_.log || []).join('\\n') || '(本条没有捕获到步骤输出)');
      } else {
        openDetail(b.closest('tr'), b.dataset.title, 'trace', async () => {
          const r = await fetch('/artifact?p=' + encodeURIComponent(case_.trace));
          return r.ok ? await r.text() : '(这条在最近一轮没有执行记录;跑一轮后可查看步骤)';
        });
      }
      return;
    }
    if (playerState.title === b.dataset.title) { closePlayer(); return; }
    closePlayer(); closeDetail();
    playerState.title = b.dataset.title;
    b.closest('tr').after(playerHost);
    document.getElementById('player-title').textContent = b.dataset.title;
    const v = document.getElementById('player-video');
    v.src = '/artifact?p=' + b.dataset.video;
    v.playbackRate = 1;
    v.play().catch(()=>{});
    playerHost.scrollIntoView({behavior:'smooth', block:'center'});
  });
  document.querySelectorAll('#rows input[type=checkbox]').forEach(cb => cb.onchange = () => {
    cb.checked ? selection.add(cb.dataset.t) : selection.delete(cb.dataset.t);
    $('b-selected').textContent = `▶ 运行选中${selection.size?` (${selection.size})`:''}`;
    $('b-selected').disabled = busy || selection.size === 0;
  });
  $('b-selected').textContent = `▶ 运行选中${selection.size?` (${selection.size})`:''}`;
  $('b-start').disabled=busy; $('b-stop').disabled=!busy;
  $('b-failed').disabled=busy||f===0;
  $('b-selected').disabled=busy||selection.size===0;
  // 直播面板:headed 运行中每 0.8s 刷新一帧
  const liveBox=$('live');
  if (['starting','running'].includes(s.status) && s.headed) {
    liveBox.style.display='block';
    const running=s.cases.filter(c=>c.state==='running').map(c=>c.title);
    $('live-title').textContent = running[0] || '准备中…';
    $('live-img').src = '/live?ts=' + Date.now();
  } else { liveBox.style.display='none'; }
  // 日志:默认收起;失败/出错自动展开;新的一轮运行开始时恢复收起;手动开关优先于自动
  if (s.status!==lastStatus) { if (s.status==='starting') { showLog=false; userToggled=false; } lastStatus=s.status; }
  if ((f>0||s.status==='error') && !userToggled) showLog=true;
  const c=$('console'), wasHidden=c.hidden;
  const nearBottom=c.scrollTop+c.clientHeight>=c.scrollHeight-40;
  c.hidden=!showLog;
  $('b-log').textContent=showLog?'▾ 收起日志':'▸ 运行日志';
  if (showLog) {
    c.innerHTML=(s.log||[]).slice(-80).map(l=>l.replace(/✓/g,'<b>✓</b>')).join('\\n');
    if (wasHidden||nearBottom) c.scrollTop=c.scrollHeight;  // 自动跟随最新,除非用户正往上翻
  }
  document.title=`${done.length}/${total} ${f?'×'+f:'✓'} · Gitwire 回归驾驶舱`;
});}
document.querySelectorAll('.fbtn[data-tf]').forEach(b => b.onclick = () => { typeFilter = b.dataset.tf; tick(); });
$('f-smoke').onclick = () => { smokeOnly = !smokeOnly; tick(); };
setInterval(tick,600); tick();
$('b-start').onclick=()=>ctl('start'); $('b-stop').onclick=()=>ctl('stop');
$('b-failed').onclick=()=>ctl('last-failed');
$('b-selected').onclick=()=>ctl('start', {selected:[...selection], headed:$('c-headed').checked, video:$('c-video').checked});
$('b-start').onclick=()=>ctl('start', {headed:$('c-headed').checked, video:$('c-video').checked});
$('b-log').onclick=()=>{showLog=!showLog;userToggled=true};
$('b-history').onclick=async()=>{
  const box=$('history');
  if(box.style.display==='block'){box.style.display='none';return;}
  box.style.display='block';
  box.textContent='加载中…';
  try{
    const h=await (await fetch('/history')).json();
    const rows=(h.recent||[]).map(e=>`<tr><td class="num">${e.at||''}</td><td>${{full:'全量',subset:'子集','last-failed':'失败项'}[e.kind]||e.kind}</td><td class="st ${e.failed?'failed':'passed'}">${e.failed?('×'+e.failed):'✓'}${e.passed!=null?(' · '+e.passed+'过'):''}</td><td class="num">${e.duration_s??''}s</td></tr>`).join('');
    const slow=(h.slow_top||[]).map(c=>`<tr><td>${c.title.slice(0,30)}</td><td class="num">${c.avg_s}s</td><td class="num">${c.runs}次</td></tr>`).join('');
    box.innerHTML=`<div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:4px">
      <span>全量轮 <strong>${h.full_runs??0}</strong></span>
      <span>成功率 <strong>${h.pass_rate!=null?(h.pass_rate*100).toFixed(0)+'%':'—'}</strong></span>
      <span>平均耗时 <strong>${h.avg_duration_s??'—'}s</strong></span></div>
      ${h.slow_top&&h.slow_top.length?`<div style="color:var(--amber);margin-top:6px">慢用例 Top(>3s,均值)</div><table><thead><tr><th>用例</th><th>均时</th><th>出现</th></tr></thead><tbody>${slow}</tbody></table>`:'<p class="meta">暂无慢用例(阈值 3s)</p>'}
      <div style="color:var(--dim);margin-top:8px">最近轮次</div>
      <table><thead><tr><th>时间</th><th>类型</th><th>结果</th><th>耗时</th></tr></thead><tbody>${rows||'<tr><td colspan="4" class="dim">还没有历史,跑一轮全量即有</td></tr>'}</tbody></table>`;
  }catch(e){box.textContent='加载失败:'+e;}
};
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code: int, body: bytes, ctype: str = 'text/html; charset=utf-8') -> None:
        self.send_response(code)
        self.send_header('content-type', ctype)
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == '/':
            self._send(200, PAGE.encode())
        elif self.path.startswith('/artifact'):
            from urllib.parse import urlparse, parse_qs, unquote
            rel = unquote(parse_qs(urlparse(self.path).query).get('p', [''])[0])
            root = (FRONTEND / '.e2e' / 'results').resolve()  # 报告里的工件路径相对 results/
            target = (root / rel).resolve()
            if not rel or not target.is_relative_to(root) or not target.is_file():
                self._send(404, b'not found')
                return
            data = target.read_bytes()
            start, end = 0, len(data) - 1
            rng = self.headers.get('range')
            if rng and rng.startswith('bytes='):
                start_s, _, end_s = rng[6:].partition('-')
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else len(data) - 1
            chunk = data[start:end + 1]
            mime = 'video/webm' if target.suffix in ('.webm', '.mp4') else 'text/markdown; charset=utf-8'
            self.send_response(206 if rng else 200)
            self.send_header('content-type', mime)
            self.send_header('content-length', str(len(chunk)))
            self.send_header('accept-ranges', 'bytes')
            if rng:
                self.send_header('content-range', f'bytes {start}-{end}/{len(data)}')
            self.end_headers()
            self.wfile.write(chunk)
        elif self.path.startswith('/live'):
            frame = LIVE_FRAME
            if frame.is_file():
                data = frame.read_bytes()
                self.send_response(200)
                self.send_header('content-type', 'image/jpeg')
                self.send_header('content-length', str(len(data)))
                self.send_header('cache-control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, b'no frame')
        elif self.path.startswith('/history'):
            entries = []
            if HISTORY.exists():
                for line in HISTORY.read_text().splitlines()[-200:]:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            full = [e for e in entries if e.get('kind') == 'full'][-20:]
            durations = [e.get('duration_s', 0) for e in full]
            slow: dict[str, list[float]] = {}
            for e in full:
                for c in e.get('slow_cases') or []:
                    slow.setdefault(c['title'], []).append(c['s'])
            self._send(200, json.dumps({
                'recent': entries[-12:][::-1],
                'full_runs': len(full),
                'pass_rate': round(sum(1 for e in full if e.get('failed') == 0) / len(full), 3) if full else None,
                'avg_duration_s': round(sum(durations) / len(durations), 1) if durations else None,
                'slow_top': sorted(
                    ({'title': t, 'avg_s': round(sum(v) / len(v), 1), 'runs': len(v)} for t, v in slow.items()),
                    key=lambda x: -x['avg_s'])[:5],
            }, ensure_ascii=False).encode(), 'application/json')
        elif self.path == '/events':
            # 微锁内完成序列化:既不持锁做网络写,也不在锁外迭代会被并发修改的活结构
            with lock:
                snapshot = json.dumps({'status': state['status'], 'total': state['total'], 'headed': state.get('headed'),
                                       'startedAt': state['startedAt'], 'cases': state['cases'],
                                       'log': list(state['log'])[-80:]}).encode()
            self._send(200, snapshot, 'application/json')
        else:
            self._send(404, b'not found')

    def do_POST(self) -> None:
        length = int(self.headers.get('content-length') or 0)
        payload = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/api/event':
            try:
                events_queue.put(payload)
            except Exception:
                import traceback
                traceback.print_exc()
            self._send(200, b'ok')
        elif self.path == '/api/control':
            action = payload.get('action')
            if action in ('start', 'last-failed'):
                if state['status'] not in ('running', 'starting'):
                    selected = payload.get('selected') or None
                    start_run('last-failed' if action == 'last-failed' else 'all', selected,
                              headed=bool(payload.get('headed')), video=bool(payload.get('video')))
            elif action == 'stop':
                stop_run()
            elif action == 'shutdown':
                self._send(200, b'bye')
                threading.Thread(target=server_ref.shutdown, daemon=True).start()
                return
            self._send(200, b'ok')
        else:
            self._send(404, b'not found')

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    busy_port = subprocess.run(['lsof', f'-tiTCP:{PORT}', '-sTCP:LISTEN'],
                               capture_output=True, text=True).stdout.strip()
    if busy_port:
        raise SystemExit(f'Port {PORT} is occupied; another dashboard running?')
    atexit.register(teardown)

    def consume_events() -> None:
        """单线程串行处理事件:请求线程只入队,永不触碰状态锁。"""
        while True:
            try:
                apply_event(events_queue.get())
            except Exception:
                import traceback
                traceback.print_exc()

    threading.Thread(target=consume_events, daemon=True).start()
    global server_ref
    server_ref = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)

    def request_stop(signum, frame):
        # shutdown() 不能在 serve_forever 所在主线程里同步调用(会死锁),转线程执行
        threading.Thread(target=server_ref.shutdown, daemon=True).start()

    # 会话结束/被终止(SIGTERM/SIGHUP)同样优雅收摊,不再依赖手动按钮
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)
    print(f'回归驾驶舱:{SELF}(关闭会话或 Ctrl-C 即自动收摊)')
    try:
        server_ref.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        teardown()


if __name__ == '__main__':
    main()
