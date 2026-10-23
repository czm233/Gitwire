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
import shutil
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
trial_proc: list[subprocess.Popen | None] = [None]
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

TRIAL_VIDEOS = ROOT / 'data' / 'ai-trial-videos.json'


def load_trial_videos() -> dict:
    """各使命卡最近一次走查的录像路径(磁盘持久化:驾驶舱重启后「回放」按钮不丢)。"""
    try:
        return {int(k): v for k, v in json.loads(TRIAL_VIDEOS.read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_trial_videos(videos: dict) -> None:
    try:
        TRIAL_VIDEOS.write_text(json.dumps(videos, ensure_ascii=False, indent=1))
    except OSError:
        pass  # 记不住录像路径不阻塞走查本身


def initial_trials() -> dict:
    """从既有试用报告解析各使命卡上次结果(## 使命 N:…· 退出码 X),并补上持久化的录像路径。
    恢复的是磁盘上的历史结果:一律带 restored 标记和报告落盘时间,前端据此显示「上次通过 · 时间」,
    不冒充本轮「已完成」(2026-10-14 用户反馈:驾驶舱刚启动,67 号使命卡就显示已完成,像偷偷跑过)。"""
    result: dict[int, dict] = {}
    report = ROOT / 'data' / 'ai-trial-report.md'
    if report.exists():
        ran_at = time.strftime('%m-%d %H:%M', time.localtime(report.stat().st_mtime))
        for number, seconds, code in re.findall(r'## 使命 (\d+):.*?· (\d+)s · 退出码 (\d+)', report.read_text()):
            result[int(number)] = {'status': 'done', 'exit': int(code), 'duration_s': int(seconds),
                                   'restored': True, 'ran_at': ran_at}
    videos = load_trial_videos()
    for charter, entry in result.items():
        v = videos.get(charter)
        if v and (ROOT / v).is_file():  # 录像文件还在才给「回放」入口
            entry['video'] = v
    return result


def new_state() -> dict:
    return {'status': 'idle', 'total': None, 'headed': False, 'startedAt': None, 'finishedAt': None,
            'trials': initial_trials(),
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
                             'interrupted': 'cancelled',  # SIGINT 打断的用例是被取消,不是测试失败
                             'blocked': 'cancelled', 'cancelled': 'cancelled', 'skipped': 'skipped'}.get(raw, 'failed')
            case['durationMs'] = dig(event, 'durationMs')
            error = dig(event, 'error') or ''
            explanation = dig(event, 'explanation') or ''
            if raw == 'interrupted':
                # 中断是用户按停止造成,不是用例问题:不展示 Playwright 内部错误对象,给一句人话
                case['note'] = '被停止指令打断,本条未跑完(不是测试失败)'
            elif error:
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


def demo_cursor_env(headed: bool, video: bool) -> dict:
    """「给人看」的运行(显示浏览器窗口/录制回放)注入演示光标并启用拟人化节奏
    (输入经 frontend/demo-pace.ts 节流,点击真实落在光标滑到之后);
    静默回归两者皆不注入,保持 DOM 与生产一致、浏览器路径与 CI 一致。"""
    if not (headed or video):
        return {}
    env = {'E2E_DEMO_CURSOR': '1', 'E2E_DEMO_PACE': '1'}
    if headed:
        env['E2E_DEMO_HEADED'] = '1'  # 演示浏览器开窗口,便于驾驶舱直播/置前跟随
    return env


def mark_trials_queued(trial_ids: list[int] | None) -> None:
    """本轮要跑的使命卡在轮次开始就整体标记「排队中」:使命卡是顺序执行的,
    不提前标记的话,跑第 1 张时其余卡显示「未运行」,看起来像漏跑了(2026-10-13 用户反馈)。"""
    with lock:
        for charter in trial_ids or []:
            state['trials'].setdefault(charter, {'status': 'idle', 'exit': None, 'duration_s': None})
            if state['trials'][charter].get('status') != 'running':
                state['trials'][charter]['status'] = 'queued'


def sweep_queued_trials(trial_ids: list[int] | None) -> None:
    """轮次结束(含被停止):仍是「排队中」的卡说明没轮到,回到「未运行」,不留僵尸状态。"""
    with lock:
        for charter in trial_ids or []:
            if state['trials'].get(charter, {}).get('status') == 'queued':
                state['trials'][charter]['status'] = 'idle'


def finished_trial(status: str, exit_code: int, duration_s: int | None, video: str) -> dict:
    """本轮真实跑完的使命卡终态:不带 restored 标记——前端只给历史恢复的结果显示「上次…」。"""
    return {'status': status, 'exit': exit_code, 'duration_s': duration_s, 'video': video}


def launch_charter(charter: int, headed: bool = False) -> int:
    """跑一张试用使命卡(状态回写 state['trials'],输出流进控制台)。
    headed 跟随驾驶舱「显示浏览器窗口」勾选(2026-10-14 用户反馈:没勾选也弹窗);
    走查录像始终录制——使命卡行内「回放」入口依赖它,与用例的「录制回放视频」勾选无关。"""
    log(f'AI 试用官:启动使命 {charter}(真实模型调用,数分钟){",可视化窗口" if headed else ",无头录像"}…')
    started = time.time()
    with lock:
        state['trials'][charter] = {'status': 'running', 'exit': None, 'duration_s': None}
    proc = subprocess.Popen(['uv', 'run', 'python', 'scripts/ai_trial.py', '--only', str(charter)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True,  # 独立进程组:dashboard 退出/停止时可整组终止
                            env={**os.environ, **demo_cursor_env(headed=headed, video=True)})  # 走查录像给人看:必带演示光标
    trial_proc[0] = proc
    for line in proc.stdout or []:
        log('AI· ' + line.rstrip())
    proc.wait()
    trial_proc[0] = None
    # 本轮走查的回放视频:explore --video 写在 results/explore-*/attempt-*/video/ 下;
    # results 会被下一轮运行清空,跑完立刻复制到 data/trial-videos/ 持久保存
    video = ''
    try:
        results = FRONTEND / '.e2e' / 'results'
        candidates = [w for d in results.glob('explore-*') if d.stat().st_mtime >= started - 1
                      for w in d.rglob('*.webm')]
        if candidates:
            src = max(candidates, key=lambda p: p.stat().st_mtime)
            dest = ROOT / 'data' / 'trial-videos' / f'mission-{charter}.webm'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dest)
            video = str(dest.relative_to(ROOT))
    except OSError:
        pass
    with lock:
        previous = state['trials'].get(charter, {}).get('duration_s')
        # 被停止/信号打断的使命卡记「已取消」:退出码 >128 是 shell 惯例的信号终止码
        # (如 130=SIGINT),与真实走查发现问题(1/2 等应用退出码)是两回事
        interrupted = proc.returncode != 0 and (stop_requested[0] or proc.returncode < 0 or proc.returncode > 128)
        status = 'cancelled' if interrupted else 'done'
        state['trials'][charter] = finished_trial(status, proc.returncode, previous, video)
    if video:
        videos = load_trial_videos(); videos[charter] = video; save_trial_videos(videos)
        # 步骤时间轴:回放时的字幕轨/跳段依据(相对视频起点约零点,近似对齐)
        try:
            from datetime import datetime
            report = json.loads((FRONTEND / '.e2e' / 'report.json').read_text())
            run = report.get('run') or {}
            start = run.get('startedAt') or ''
            if start:
                t0 = datetime.fromisoformat(start.replace('Z', '+00:00')).timestamp()
                steps = []
                for i, step in enumerate((run.get('explore') or {}).get('steps') or []):
                    at = step.get('startedAt') or ''
                    if not at:
                        continue
                    rel = datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp() - t0
                    steps.append({'idx': i + 1, 't': round(max(rel, 0), 1),
                                  'dur': round((step.get('durationMs') or 0) / 1000, 1),
                                  'title': (step.get('title') or '')[:60], 'status': step.get('status') or ''})
                (ROOT / 'data' / 'trial-videos' / f'mission-{charter}.meta.json').write_text(
                    json.dumps({'steps': steps}, ensure_ascii=False))
        except (OSError, ValueError, KeyError):
            pass  # 时间轴缺失只影响字幕,不影响录像本身
    log(f'AI 试用官:使命 {charter} 结束(退出码 {proc.returncode}),报告已更新。')
    return proc.returncode


def start_run(mode: str, selected: list[str] | None = None, headed: bool = False, video: bool = False,
              trial_ids: list[int] | None = None) -> None:
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
        # 使命卡先整体入队:回归/走查全程可见「排队中」,不再是"只看到第一张在跑"
        mark_trials_queued(trial_ids)
        # 只勾了使命卡时跳过用例回归:此前会先整跑全部用例才轮到使命卡(2026-10-13 用户反馈);
        # 每张使命卡由 ai_trial.py 自带隔离栈起收,连驾驶舱预拉的隔离栈都不需要
        trials_only = bool(trial_ids) and not selected
        proc = None
        if not trials_only:
            log('拉起 e2e 隔离栈(10248/10249)…')
            up = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'up'],
                                cwd=ROOT, capture_output=True, text=True)
            if up.returncode:
                with lock:
                    state['status'] = 'error'
                log('隔离栈启动失败:' + (up.stdout + up.stderr).strip()[-300:])
                sweep_queued_trials(trial_ids)  # 已标「排队中」的卡收回到「未运行」
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
            if trial_ids:
                log(f'用例跑完后接着顺序执行 {len(trial_ids)} 张试用使命卡…')
            # --trace:每条用例都留步骤记录(官方默认只写失败的),供「步骤」按钮查看
            extras += ['--trace']
            args = ['npm', 'run', 'e2e'] + (['--', *extras] if extras else [])
            run_env = {**os.environ, 'E2E_DASHBOARD': f'{SELF}/api/event', **demo_cursor_env(headed, video)}
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
        else:
            log(f'只勾选了 {len(trial_ids)} 张使命卡:跳过用例回归,顺序开始 AI 走查…')
        runner_proc[0] = None
        # 「运行选中」混选时:用例跑完接着顺序跑使命卡;停止指令同样终止剩余使命卡
        for charter in trial_ids or []:
            if stop_requested[0]:
                log(f'已停止:使命 {charter} 及其后续不再运行')
                break
            launch_charter(charter, headed)
        sweep_queued_trials(trial_ids)
        # 先在锁内对案例做快照:下一轮此刻可能已 rescan 重置共享 state
        with lock:
            if state['status'] in ('running', 'starting'):
                # 停止优先于退出码;只跑使命卡(proc 为 None)正常走完算 finished
                state['status'] = ('stopped' if stop_requested[0]
                                   else 'finished' if (proc is None or proc.returncode == 0)
                                   else 'error')
                state['finishedAt'] = time.time()
            snapshot = [dict(c) for c in state['cases']]
            final_status = state['status']
        counts = {s: sum(1 for c in snapshot if c['state'] == s) for s in ('passed', 'failed')}
        log(f"运行结束:通过 {counts['passed']} · 失败 {counts['failed']}"
            f"(退出码 {proc.returncode if proc else '—'})")
        # 历史落盘:本轮摘要(局部时间+快照统计,与共享 state 解耦);
        # 只跑使命卡的轮次不记入——它没有用例结果,记了会污染趋势页的成功率统计
        if not trials_only:
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

def run_single_trial(charter: int, headed: bool = False) -> None:
    """单卡「▶ 运行」的线程体:走完整状态机,让「停止」按钮在单卡运行期间可用。
    (2026-10-15 用户反馈:使命卡单独运行后停不下来——此前 state['status'] 一直停在
    idle,前端 busy 恒为 false,停止按钮始终禁用,只能干等几分钟跑完。)"""
    with lock:
        state['status'] = 'running'
        state['startedAt'] = time.time()
        state['finishedAt'] = None
    try:
        launch_charter(charter, headed)
    finally:
        with lock:
            state['status'] = 'stopped' if stop_requested[0] else 'finished'
            state['finishedAt'] = time.time()
        stop_requested[0] = False  # 单卡路径不经过 start_run 的收尾,这里自己复位


def stop_run() -> None:
    # 标志位始终先设:使命卡阶段的循环靠它不再启动后续卡,即使当前没有可杀的进程
    stop_requested[0] = True
    proc = runner_proc[0] or trial_proc[0]
    if not proc:
        return
    log('收到停止指令,正在收尾(等待运行器优雅退出)…')
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass

def teardown() -> None:
    if runner_proc[0] or trial_proc[0]:
        stop_run()
        for _ in range(50):
            if runner_proc[0] is None and trial_proc[0] is None:
                break
            time.sleep(0.2)
    if stack_owned[0]:
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'down'], cwd=ROOT,
                       capture_output=True)
        stack_owned[0] = False

PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Gitwire 回归驾驶舱</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+SC:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  /* 设计 token 与主站 styles.css 完全一致 */
  :root{
    --bg:#0a0f14;--bg-raised:#0e151d;--bg-card:#101923;--bg-inset:#0b1218;
    --line:#1c2833;--line-bright:#2a3b4a;--ink:#c8d6e0;--ink-dim:#8fa5b8;--ink-faint:#47586a;
    --green:#7ef2a8;--green-dim:#3d8f63;--amber:#ffb454;--amber-dim:#8f6a3a;--red:#ff7a7a;--red-dim:#8f4444;--cyan:#6fd8e8;
    --panel:var(--bg-card);--dim:var(--ink-dim);--faint:var(--ink-faint);
    --display:"Chakra Petch","PingFang SC","Microsoft YaHei",sans-serif;
    --body:"IBM Plex Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{
    margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.6 var(--body);-webkit-font-smoothing:antialiased;
  }
  /* 氛围:顶部辉光 + 扫描线 */
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background:radial-gradient(1200px 500px at 70% -10%,rgba(126,242,168,.055),transparent 60%),
               radial-gradient(900px 400px at 10% 110%,rgba(111,216,232,.04),transparent 60%),
               repeating-linear-gradient(0deg,transparent 0 2px,rgba(255,255,255,.008) 2px 4px);
  }
  /* 氛围:取景框网格 */
  body::after{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:44px 44px;opacity:.14;
    mask-image:radial-gradient(ellipse at 50% 0%,black 0%,transparent 75%);
  }
  ::selection{background:rgba(126,242,168,.25)}
  ::-webkit-scrollbar{width:6px;height:6px}
  ::-webkit-scrollbar-track{background:var(--bg)}
  ::-webkit-scrollbar-thumb{background:var(--line-bright);border-radius:3px}
  ::-webkit-scrollbar-thumb:hover{background:var(--green-dim)}

  /* 卡片:取景框四角刻度 */
  .card{position:relative;background:var(--bg-card);border:1px solid var(--line);padding:18px 20px}
  .card::before,.card::after,.card>.tick-b::before,.card>.tick-b::after{
    content:"";position:absolute;width:10px;height:10px;border-color:var(--line-bright);border-style:solid;
    transition:border-color .2s,width .2s,height .2s;
  }
  .card::before{top:-1px;left:-1px;border-width:2px 0 0 2px}
  .card::after{top:-1px;right:-1px;border-width:2px 2px 0 0}
  .card>.tick-b::before{bottom:-1px;left:-1px;border-width:0 0 2px 2px}
  .card>.tick-b::after{bottom:-1px;right:-1px;border-width:0 2px 2px 0}
  .card:hover::before,.card:hover::after,.card:hover>.tick-b::before,.card:hover>.tick-b::after{
    border-color:var(--green);width:15px;height:15px;
  }

  /* 标题与顶部栏 */
  .page-head{display:flex;align-items:center;justify-content:space-between;gap:16px;user-select:none;flex-wrap:wrap;margin-bottom:18px}
  .page-head-left{display:flex;align-items:center;gap:12px;min-width:0}
  .page-title{font-family:var(--display);font-size:22px;margin:0;font-weight:600;letter-spacing:.06em;color:#e8f2ec}
  .page-title::before{content:"// ";color:var(--green-dim)}
  .help-btn{
    width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;
    font-family:var(--mono);font-size:12px;font-weight:600;color:var(--ink-dim);border:1px solid var(--line-bright);
    background:var(--bg-inset);padding:0;cursor:pointer;transition:all .15s;
  }
  .help-btn:hover{color:var(--green);border-color:var(--green);background:rgba(126,242,168,.1);box-shadow:0 0 8px rgba(126,242,168,.25)}
  .pill{
    display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border:1px solid var(--line-bright);
    background:var(--bg-inset);border-radius:99px;font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--ink);
  }
  .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
  .pill.run{color:var(--green);border-color:var(--green-dim);background:rgba(126,242,168,.08)}
  .pill.run::before{animation:pulse 1s infinite}
  .pill.bad{color:var(--red);border-color:var(--red-dim);background:rgba(255,122,122,.08)}

  /* 顶部操作工具栏 */
  .page-actions{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .opt-group{display:flex;align-items:center;gap:14px;user-select:none}
  .opt-label{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:12px;color:var(--ink);cursor:pointer}
  .opt-label input{accent-color:var(--green);cursor:pointer}
  .btn-group{display:flex;align-items:center;gap:8px}

  /* 按钮系统 */
  .btn{
    display:inline-flex;align-items:center;justify-content:center;gap:6px;font-family:var(--mono);
    font-size:12px;line-height:1.2;letter-spacing:.04em;white-space:nowrap;padding:6px 14px;
    border-radius:3px;cursor:pointer;transition:all .15s;background:none;border:1px solid var(--line);color:var(--ink);
  }
  .btn:hover{border-color:var(--green);color:var(--green)}
  .btn:disabled{opacity:.35;cursor:not-allowed;box-shadow:none!important}
  .btn-primary{
    color:#07130c;background:var(--green);border-color:var(--green);font-weight:600;
    box-shadow:0 0 12px rgba(126,242,168,.25);
  }
  .btn-primary:hover{background:#9ef7be;border-color:#9ef7be;box-shadow:0 0 18px rgba(126,242,168,.4);color:#040d08}
  .btn-primary:disabled{background:rgba(126,242,168,.2);color:var(--ink-faint);border-color:var(--line-bright)}
  .btn-ghost{color:var(--green);border-color:var(--green-dim);background:rgba(126,242,168,.05)}
  .btn-ghost:hover{background:rgba(126,242,168,.14);border-color:var(--green);box-shadow:0 0 12px rgba(126,242,168,.15)}
  .btn-ghost:disabled{border-color:var(--line);color:var(--ink-faint);background:transparent}
  .btn-danger{color:var(--red);border-color:var(--red-dim);background:rgba(255,122,122,.06)}
  .btn-danger:hover{background:rgba(255,122,122,.15);border-color:var(--red);box-shadow:0 0 12px rgba(255,122,122,.2)}
  .btn-danger:disabled{border-color:var(--line);color:var(--ink-faint);background:transparent}

  /* 统计条:大数 + 标签 + 联动筛选 */
  .stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:16px 0 6px}
  .stat{cursor:pointer;transition:border-color .15s,background .15s;padding:14px 16px;user-select:none}
  .stat:hover{border-color:var(--line-bright);background:var(--bg-raised)}
  .stat.active{border-color:var(--green-dim);background:rgba(126,242,168,.06);box-shadow:inset 2px 0 0 var(--green)}
  .stat.active::before,.stat.active::after,.stat.active>.tick-b::before,.stat.active>.tick-b::after{
    border-color:var(--green);width:14px;height:14px;
  }
  .stat.active .label{color:var(--green-dim);font-weight:600}
  .stat .num{font-family:var(--display);font-size:32px;font-weight:700;color:var(--green);line-height:1.1}
  .stat .num.red{color:var(--red)} .stat .num.amber{color:var(--amber)} .stat .num.dim{color:var(--ink-dim)}
  .stat.active .num{text-shadow:0 0 14px rgba(126,242,168,.35)}
  .stat.active .num.red{text-shadow:0 0 14px rgba(255,122,122,.35)}
  .stat .label{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink);margin-top:6px}

  /* 遥测进度条 */
  .bar{height:6px;background:var(--bg-inset);border:1px solid var(--line);border-radius:3px;margin:16px 0 8px;overflow:hidden;position:relative}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--green-dim),var(--green));width:0;transition:width .35s ease;box-shadow:0 0 8px rgba(126,242,168,.4)}
  .bar>i.bad{background:linear-gradient(90deg,var(--red-dim),var(--red));box-shadow:0 0 8px rgba(255,122,122,.4)}

  /* 筛选工具条 */
  .fbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0 10px;font-size:12px;color:var(--ink);user-select:none}
  .gsel{position:relative}
  .gsel-btn{
    display:flex;gap:8px;align-items:center;font-family:var(--mono);font-size:12px;padding:5px 12px;
    border-radius:3px;border:1px solid var(--line-bright);background:var(--bg-inset);color:var(--ink);cursor:pointer;transition:all .15s;
  }
  .gsel-btn:hover{border-color:var(--green-dim)}
  .gsel-btn .lab{color:var(--ink-dim)} .gsel-btn .val{color:var(--ink);font-weight:600} .gsel-btn .car{color:var(--ink-dim);font-size:10px}
  .gsel-list{
    position:absolute;top:calc(100% + 4px);left:0;z-index:40;display:flex;flex-direction:column;
    background:var(--bg-card);border:1px solid var(--line-bright);border-radius:3px;padding:4px;min-width:120px;
    box-shadow:0 12px 32px rgba(0,0,0,.65);backdrop-filter:blur(8px);
  }
  .gsel-list[hidden]{display:none}
  .gsel-opt{
    text-align:left;border:1px solid transparent;background:none;padding:5px 10px;border-radius:2px;
    font-family:var(--mono);font-size:12px;color:var(--ink);white-space:nowrap;cursor:pointer;transition:all .1s;
  }
  .gsel-opt:hover{background:rgba(126,242,168,.08);color:var(--green)}
  .gsel-opt.on{color:var(--green);font-weight:600;background:rgba(126,242,168,.05)}
  .search-wrap{position:relative;display:flex;align-items:center}
  .search-wrap input{
    background:var(--bg-inset);border:1px solid var(--line-bright);border-radius:3px;color:var(--ink);
    font-family:var(--mono);font-size:12px;padding:5px 28px 5px 12px;user-select:text;outline:none;width:230px;transition:all .15s;
  }
  .search-wrap input:focus{border-color:var(--green);box-shadow:0 0 10px rgba(126,242,168,.15)}
  .search-wrap input::placeholder{color:var(--ink-faint)}
  .search-clear{position:absolute;right:8px;background:none;border:none;color:var(--ink-dim);font-size:14px;cursor:pointer;padding:0;line-height:1}
  .search-clear:hover{color:var(--ink)}
  .f-count{font-family:var(--mono);font-size:12px;color:var(--ink);letter-spacing:.02em}

  /* 表格卡片容器 */
  .tbl-card{position:relative;background:var(--bg-card);border:1px solid var(--line);margin-top:10px;padding:0;overflow:hidden}
  table.tbl{width:100%;border-collapse:collapse;font-size:13px}
  table.tbl th{
    text-align:left;padding:10px 14px;background:rgba(14,21,29,.75);border-bottom:1px solid var(--line-bright);
    font-family:var(--mono);font-size:12px;font-weight:600;color:var(--ink);white-space:nowrap;user-select:none;letter-spacing:.04em;
  }
  table.tbl td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top;color:var(--ink)}
  table.tbl tr:last-child td{border-bottom:none}
  table.tbl tbody tr{transition:background .12s}
  table.tbl tbody tr:hover{background:rgba(126,242,168,.035)}
  table.tbl tbody tr.row-selected{background:rgba(126,242,168,.06)}
  td.num,th.num{font-family:var(--mono);color:var(--ink);white-space:nowrap}
  .nw{white-space:nowrap}
  .case-title{font-weight:500;color:var(--ink);line-height:1.45}

  /* 状态指示徽章 */
  .st{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:12px;white-space:nowrap}
  .st .st-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
  .st.passed{color:var(--green)} .st.passed .st-dot{box-shadow:0 0 6px var(--green)}
  .st.failed{color:var(--red)} .st.failed .st-dot{box-shadow:0 0 6px var(--red)}
  .st.running{color:var(--amber)} .st.running .st-dot{animation:pulse 1s infinite;box-shadow:0 0 6px var(--amber)}
  .st.queued{color:var(--amber)}
  .st.cancelled,.st.pending,.st.skipped{color:var(--ink-dim)}

  /* 标签 */
  .tag{
    display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:2px;
    font-family:var(--mono);font-size:11px;letter-spacing:.04em;white-space:nowrap;user-select:none;vertical-align:middle;
  }
  .tag::before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}
  .tag.det{color:var(--ink);border:1px solid var(--line-bright);background:var(--bg-inset)}
  .tag.ai{color:var(--amber);border:1px solid var(--amber-dim);background:rgba(255,180,84,.07)}
  .tag.smoke{color:var(--green);border:1px solid var(--green-dim);background:rgba(126,242,168,.07);margin-left:5px}
  .tag.trial{color:var(--cyan);border:1px solid #2a5a64;background:rgba(111,216,232,.07)}

  /* 备注与错误警报 */
  .note{
    margin-top:6px;padding:5px 10px;border-radius:2px;font-family:var(--mono);font-size:11px;line-height:1.5;
    color:var(--red);background:rgba(255,122,122,.07);border-left:3px solid var(--red);word-break:break-all;
    display:flex;align-items:flex-start;gap:6px;
  }
  .note .note-tag{
    flex-shrink:0;display:inline-block;padding:1px 4px;border-radius:2px;font-size:9px;font-weight:700;
    letter-spacing:.05em;background:var(--red);color:#1a0505;margin-top:2px;
  }
  .note.cancelled{color:var(--ink-dim);background:rgba(143,165,184,.07);border-left-color:var(--ink-dim)}
  .note.cancelled .note-tag{background:var(--ink-dim);color:#0d151d}
  .info{
    margin-top:5px;padding:4px 8px;border-radius:2px;font-family:var(--mono);font-size:11px;line-height:1.5;
    color:var(--ink-dim);background:rgba(111,216,232,.05);border-left:3px solid var(--cyan);word-break:break-all;
    display:flex;align-items:flex-start;gap:6px;
  }
  .info .info-tag{
    flex-shrink:0;display:inline-block;padding:1px 4px;border-radius:2px;font-size:9px;font-weight:700;
    letter-spacing:.05em;background:var(--cyan);color:#041215;margin-top:2px;
  }

  /* 行内操作按钮 */
  .row-actions{display:flex;align-items:center;gap:4px;white-space:nowrap}
  .btn-row{
    display:inline-flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:11px;
    padding:3px 9px;border-radius:2px;cursor:pointer;white-space:nowrap;transition:all .15s;
    border:1px solid var(--line-bright);background:var(--bg-inset);color:var(--ink);
  }
  .btn-row:hover{border-color:var(--ink-dim);color:#fff;background:rgba(200,214,224,.1)}
  .btn-row:disabled{opacity:.35;cursor:not-allowed;border-color:var(--line);color:var(--ink-faint);background:transparent}
  .btn-row.row-run,.btn-row.trial-run{color:var(--green);border-color:var(--green-dim);background:rgba(126,242,168,.05)}
  .btn-row.row-run:hover,.btn-row.trial-run:hover{background:rgba(126,242,168,.15);border-color:var(--green);box-shadow:0 0 10px rgba(126,242,168,.2)}
  .btn-row.view-vid{color:var(--cyan);border-color:#2a5a64;background:rgba(111,216,232,.05)}
  .btn-row.view-vid:hover{border-color:var(--cyan);color:#a4f3ff;box-shadow:0 0 10px rgba(111,216,232,.2)}
  .btn-row.view-trial{color:var(--amber);border-color:var(--amber-dim);background:rgba(255,180,84,.05)}
  .btn-row.view-trial:hover{border-color:var(--amber);color:#ffe0b3;box-shadow:0 0 10px rgba(255,180,84,.2)}

  /* 分页条:同主站 ListPager */
  .pbar{display:flex;gap:12px;align-items:center;margin-top:14px;font-family:var(--mono);font-size:12px;color:var(--ink);user-select:none}
  .pgbtn{padding:3px 10px;border:1px solid var(--line-bright);background:var(--bg-inset);color:var(--ink);font-family:var(--mono);font-size:12px;border-radius:2px;cursor:pointer;transition:all .15s}
  .pgbtn:hover{border-color:var(--green);color:var(--green)}
  .pgbtn.on{color:var(--green);border-color:var(--green);background:rgba(126,242,168,.12);font-weight:600;box-shadow:0 0 8px rgba(126,242,168,.2)}

  /* 直播面板 */
  #live{display:none;margin-top:18px;background:var(--bg-card);border:1px solid var(--amber-dim);padding:14px 18px;position:relative}
  .live-head{display:flex;gap:10px;align-items:center;margin-bottom:10px}
  .live-badge{
    color:var(--amber);font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.1em;
    padding:2px 8px;border:1px solid var(--amber-dim);border-radius:2px;background:rgba(255,180,84,.1);animation:pulse 1.4s infinite;
  }
  .live-title{font-family:var(--mono);font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .live-frame{border:1px solid var(--line-bright);background:#000;border-radius:2px;overflow:hidden}
  .live-frame img{width:100%;display:block}

  /* 详情与帮助 Modal */
  .modal-mask{
    position:fixed;inset:0;z-index:50;background:rgba(4,8,12,.68);backdrop-filter:blur(4px);
    display:flex;align-items:center;justify-content:center;padding:32px;
  }
  .modal-mask[hidden]{display:none}
  .modal-box{
    max-width:980px;width:100%;max-height:85vh;display:flex;flex-direction:column;
    box-shadow:0 24px 80px rgba(0,0,0,.7);padding:0;overflow:hidden;border:1px solid var(--line-bright);
  }
  .modal-head{display:flex;gap:12px;align-items:center;padding:12px 18px;background:rgba(14,21,29,.85);border-bottom:1px solid var(--line-bright);flex-wrap:wrap}
  .modal-head strong{font-family:var(--display);font-size:15px;font-weight:600;letter-spacing:.04em;color:#fff}
  .dmode{
    font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:2px;color:var(--ink-dim);
    border:1px solid var(--line);background:var(--bg-inset);cursor:pointer;transition:all .15s;
  }
  .dmode:hover{color:var(--ink);border-color:var(--line-bright)}
  .dmode.on{color:var(--green);border-color:var(--green-dim);background:rgba(126,242,168,.08);font-weight:600;box-shadow:0 0 8px rgba(126,242,168,.15)}
  .modal-body{overflow:auto;padding:16px 18px;flex:1;min-height:0}
  .modal-speed{display:flex;gap:6px;align-items:center;font-family:var(--mono);font-size:11px;color:var(--ink-dim)}
  .modal-speed[hidden]{display:none}
  .modal-speed button{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:2px;border:1px solid var(--line);background:var(--bg-inset);color:var(--ink);cursor:pointer}
  .modal-speed button:hover{border-color:var(--green);color:var(--green)}
  #speed{width:160px;accent-color:var(--green)}
  .detail-pre{
    max-height:520px;overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.6;
    color:var(--ink);white-space:pre-wrap;background:var(--bg-inset);border:1px solid var(--line-bright);border-radius:3px;padding:14px 16px;margin:0;
  }
  .vid-wrap{position:relative}
  .vid-wrap[hidden]{display:none}
  .vid-wrap.fast video{border-radius:4px;box-shadow:0 0 0 1px var(--amber),0 0 18px rgba(255,180,84,.25)}
  .vid-wrap video{width:100%;border-radius:4px;background:#000;display:block}
  .ff-badge{
    position:absolute;top:12px;right:12px;z-index:5;display:flex;flex-direction:column;gap:2px;
    align-items:flex-end;padding:6px 12px;border:1px solid var(--amber);border-radius:6px;
    background:rgba(14,21,29,.85);backdrop-filter:blur(6px);color:var(--amber);text-align:right;
    pointer-events:none;opacity:0;transition:opacity .25s;animation:ff-pulse 1.6s ease-in-out infinite;font-family:var(--mono);
  }
  .ff-badge.show{opacity:1}
  .ff-badge .main{font-size:12px;font-weight:700;letter-spacing:.08em}
  .ff-badge .sub{font-size:11px;color:var(--ink-dim)}
  @keyframes ff-pulse{50%{box-shadow:0 0 14px rgba(255,180,84,.35)}}
  .tl{display:flex;gap:2px;margin-top:8px;height:16px;position:relative}
  .tl[hidden]{display:none}
  .tl-seg{background:var(--green-dim);cursor:pointer;min-width:6px;border-radius:2px;transition:filter .15s}
  .tl-seg:hover{filter:brightness(1.3)}
  .tl-seg.warn{background:var(--amber)} .tl-seg.bad{background:var(--red)}
  .tl-seg.cur{outline:1px solid var(--ink);filter:brightness(1.5)}
  .tl-head{position:absolute;top:-3px;bottom:-3px;width:2px;background:#fff;border-radius:1px;box-shadow:0 0 8px #fff;pointer-events:none;transform:translateX(-1px);z-index:2}
  .tcap{font-family:var(--mono);font-size:12px;color:var(--ink-dim);margin-top:8px;min-height:18px}
  .tcap[hidden]{display:none}
  .tl-act{position:relative;height:6px;margin-top:4px;background:var(--bg-inset);border-radius:3px}
  .tl-act[hidden]{display:none}
  .tl-act .tl-bar{position:absolute;top:0;bottom:0;background:var(--green);border-radius:3px}
  .fopt{display:inline-flex;align-items:center;gap:6px;margin-top:8px;font-family:var(--mono);font-size:12px;color:var(--ink-dim);cursor:pointer;user-select:none}
  .fopt input{accent-color:var(--green)}

  /* 帮助弹窗 */
  .help-body{display:flex;flex-direction:column;gap:18px;font-size:13px;line-height:1.7}
  .help-sec h3{font-family:var(--display);font-size:14px;color:var(--green);margin:0 0 6px;letter-spacing:.04em}
  .help-sec p{margin:0;color:var(--ink)}
  .help-tbl{width:100%;border-collapse:collapse;margin-top:6px}
  .help-tbl td{padding:6px 8px;border-bottom:1px solid var(--line);font-size:12px;color:var(--ink);vertical-align:middle}
  .help-tbl tr:last-child td{border-bottom:none}

  /* 应用骨架与侧栏 */
  .app{position:relative;z-index:1;display:grid;grid-template-columns:216px 1fr;min-height:100vh}
  .side{
    border-right:1px solid var(--line);padding:20px 14px;position:sticky;top:0;height:100vh;
    display:flex;flex-direction:column;gap:4px;background:rgba(10,15,20,.72);backdrop-filter:blur(6px);
    user-select:none;-webkit-user-select:none;
  }
  .side a{-webkit-user-drag:none}
  .brand{
    font-family:var(--display);font-weight:700;font-size:20px;letter-spacing:.16em;color:var(--green);
    text-shadow:0 0 18px rgba(126,242,168,.35);padding:4px 8px 14px;display:flex;align-items:baseline;gap:8px;
  }
  .brand .cursor{display:inline-block;width:9px;height:18px;background:var(--green);animation:blink 1.1s steps(1) infinite;margin-left:2px}
  @keyframes blink{50%{opacity:0}}
  .brand-sub{font-size:10px;letter-spacing:.3em;color:var(--ink-faint);font-family:var(--mono);padding:0 8px 16px;margin-top:-10px}
  .nav{
    display:flex;gap:8px;align-items:baseline;padding:8px 12px;border:1px solid transparent;border-left:2px solid transparent;
    color:var(--ink-dim);text-decoration:none;font-family:var(--mono);font-size:13px;transition:all .15s;
  }
  .nav:hover{color:var(--ink);border-color:var(--line);text-decoration:none}
  .nav.on{color:var(--green);border-color:var(--green-dim);background:rgba(126,242,168,.06);box-shadow:inset 2px 0 0 var(--green)}
  .nav.on .no{color:var(--green-dim)}
  .nav .no{font-size:10px;color:var(--ink-faint)}
  .slogan{margin-top:auto;font-family:var(--mono);font-size:11px;color:var(--ink-faint);padding:10px;line-height:1.5}
  .wrap{flex:1;min-width:0;padding:26px 36px 60px;width:100%}

  /* 趋势页 */
  .slowrow{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:12px;font-family:var(--mono)}
  .slowrow .name{flex-shrink:0;width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
  .slowrow .track{flex:1;height:6px;background:var(--bg-inset);border-radius:3px;overflow:hidden}
  .slowrow .track i{display:block;height:100%;background:var(--amber);border-radius:3px}
  .slowrow .num{color:var(--ink-dim);white-space:nowrap;width:86px;text-align:right}
  .sec{margin-top:24px;font-family:var(--display);font-size:14px;font-weight:600;color:var(--ink);letter-spacing:.04em}
  .sec::before{content:"// ";color:var(--green)}
  .sec small{font-weight:400;color:var(--ink-dim);font-family:var(--mono);font-size:11px;margin-left:8px}
  .meta{color:var(--ink-dim);font-family:var(--mono);font-size:12px}
  @keyframes pulse{50%{opacity:.4}}
</style></head><body><div class="app">
<nav class="side">
  <div class="brand">⟨GITWIRE⟩<span class="cursor"></span></div>
  <div class="brand-sub">回归驾驶舱</div>
  <a class="nav" href="#/trends"><span class="no">01</span>趋势</a>
  <a class="nav on" href="#/run"><span class="no">02</span>回归</a>
  <div class="slogan">关闭会话即自动收摊</div>
</nav>
<main class="wrap">
<section id="view-run">
<header class="page-head">
  <div class="page-head-left">
    <h1 class="page-title">回归</h1>
    <button class="help-btn" id="b-help" title="查看回归驾驶舱帮助与说明">?</button>
    <span class="pill" id="pill">—</span>
  </div>
  <div class="page-actions">
    <div class="opt-group">
      <label class="opt-label" title="可视化运行:浏览器置前 + 驾驶舱直播;自动注入演示光标(蓝色点击涟漪),操作肉眼可见;对用例与使命卡同样生效,不勾选则全程无头"><input type="checkbox" id="c-headed"><span>显示浏览器窗口</span></label>
      <label class="opt-label" title="每条用例录制 webm 回放(自动带演示光标),跑完在行内操作列点「回放」观看"><input type="checkbox" id="c-video"><span>录制回放视频</span></label>
    </div>
    <div class="btn-group">
      <button id="b-start" class="btn btn-primary" title="运行全部测试案例与使命卡">▶ 全量回归</button>
      <button id="b-selected" class="btn btn-ghost" disabled title="运行勾选的项目">▶ 运行选中</button>
      <button id="b-failed" class="btn btn-ghost" disabled title="只跑上轮失败项">↻ 只跑失败项</button>
      <button id="b-stop" class="btn btn-danger" disabled title="停止正在运行的测试">■ 停止</button>
    </div>
  </div>
</header>
<div class="stats-row" id="stats"></div>
<div class="bar"><i id="fill"></i></div>
<div class="fbar">
  <div class="gsel" id="gs-type">
    <button class="gsel-btn" type="button"><span class="lab">类型</span><span class="val">全部</span><span class="car">▾</span></button>
    <div class="gsel-list" hidden>
      <button class="gsel-opt on" data-v="all">全部</button>
      <button class="gsel-opt" data-v="det">确定性</button>
      <button class="gsel-opt" data-v="ai">AI·模型</button>
      <button class="gsel-opt" data-v="setup">前置</button>
      <button class="gsel-opt" data-v="smoke" title="只看带「冒烟」标签的用例:check.py smoke 快速门禁跑的就是这个子集">冒烟</button>
    </div>
  </div>
  <div class="search-wrap">
    <input id="f-search" placeholder="按标题文字筛选..." title="包含匹配,不区分大小写;同时作用于用例与使命卡标题">
    <button id="f-clear" class="search-clear" title="清空搜索" type="button" hidden>×</button>
  </div>
  <span style="flex:1"></span>
  <span class="f-count" id="filter-count"></span>
</div>
<div class="tbl-card card"><span class="tick-b"></span>
<table class="tbl">
  <thead>
    <tr>
      <th style="width:40px;text-align:center"><input type="checkbox" id="c-all" title="全选当前筛选结果(跨页:含未显示到本页的行;筛选变化不影响已选中的其他行)"></th>
      <th style="width:52px" class="num">#</th>
      <th>测试案例</th>
      <th style="width:110px" class="nw">类型</th>
      <th style="width:120px" class="nw">状态</th>
      <th style="width:84px" class="num nw">耗时</th>
      <th style="width:168px" class="nw">操作</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
</div>
<div class="pbar" id="pbar"><span id="p-total"></span>
  <div class="gsel" id="gs-size">
    <button class="gsel-btn" type="button"><span class="lab">每页</span><span class="val">10</span><span class="car">▾</span></button>
    <div class="gsel-list" hidden>
      <button class="gsel-opt on" data-v="10">10 条</button>
      <button class="gsel-opt" data-v="25">25 条</button>
      <button class="gsel-opt" data-v="50">50 条</button>
    </div>
  </div>
  <span style="flex:1"></span><span id="p-pages"></span></div>
<div id="live" class="card"><span class="tick-b"></span>
  <div class="live-head">
    <span class="live-badge">● LIVE</span>
    <strong id="live-title" class="live-title"></strong>
  </div>
  <div class="live-frame">
    <img id="live-img" alt="测试浏览器实时画面">
  </div>
</div>
</section>
<div class="modal-mask" id="modal" hidden>
  <div class="card modal-box"><span class="tick-b"></span>
    <div class="modal-head"><strong id="modal-title"></strong><span id="modal-modes"></span>
      <span style="flex:1"></span>
      <span class="modal-speed" id="modal-speed" hidden>
        <input type="range" id="speed" min="0.1" max="3" step="0.05" value="1"><span id="speed-label" style="color:var(--green)">1×</span>
        <button data-rate="0.5">0.5×</button><button data-rate="1">1×</button><button data-rate="2">2×</button>
      </span>
      <button id="modal-close" class="btn btn-danger" style="padding:3px 10px;font-size:11px">关闭</button></div>
    <div class="modal-body"><pre class="detail-pre" id="modal-pre" style="border:none;padding:0;margin:0"></pre>
      <div class="vid-wrap" id="vid-wrap">
        <video id="modal-video" controls hidden></video>
        <div class="ff-badge" id="ff-badge"><span class="main">空闲快进 ×10</span><span class="sub" id="ff-countdown"></span></div>
      </div>
      <div id="modal-caption" class="tcap" hidden></div>
      <div id="modal-timeline" class="tl" hidden></div>
      <div id="modal-act" class="tl-act" hidden title="亮色段=操作段(步骤时间轴+画面变化检测取并集);暗色段=空闲等待 AI 回复,播放时自动快进"></div>
      <label class="fopt" id="auto-skip-box" hidden title="空闲段自动 10 倍快进;前瞻式调速——按当前倍速预估,操作段开始前约 1 秒(墙钟)就降回正常速度,段前光标移动与点击全程可见"><input type="checkbox" id="auto-skip" checked>空闲自动快进</label></div>
  </div>
</div>
<div class="modal-mask" id="help-modal" hidden>
  <div class="card modal-box" style="max-width:760px"><span class="tick-b"></span>
    <div class="modal-head">
      <strong>// 回归驾驶舱 · 使用指南</strong>
      <span style="flex:1"></span>
      <button id="help-modal-close" class="btn btn-ghost" style="padding:3px 10px;font-size:11px">关闭</button>
    </div>
    <div class="modal-body help-body">
      <div class="help-sec">
        <h3>01 // 驾驶舱定位</h3>
        <p>Gitwire 自动化回归与 AI 走查中枢。实时查看用例与使命卡执行状态、步骤时间轴、录像回放，支持全量回归、任意子集勾选重跑、失败项极速复测与优雅停止。</p>
      </div>
      <div class="help-sec">
        <h3>02 // 测试用例体系</h3>
        <table class="help-tbl">
          <tr><td class="nw" style="width:90px"><span class="tag det">确定性</span></td><td>Playwright 确定性断言用例，毫秒级执行，用于快速验证核心链路与回归门禁。</td></tr>
          <tr><td class="nw"><span class="tag ai">AI·模型</span></td><td>LLM 驱动的智能断言用例，用于验证复杂界面状态、推理分析与智能生成输出。</td></tr>
          <tr><td class="nw"><span class="tag smoke">冒烟</span></td><td>门禁精选子集（<code>scripts/check.py smoke</code>），在 30 秒内极速验证关键链路。</td></tr>
          <tr><td class="nw"><span class="tag det">前置</span></td><td>测试环境初始化任务（如数据库重置与种子数据装填），运行特定用例时自动依序触发。</td></tr>
          <tr><td class="nw"><span class="tag trial">AI 试用</span></td><td>AI 试用官使命卡，在隔离栈中模拟访客/新用户等角色全自动走查真实浏览器页面。</td></tr>
        </table>
      </div>
      <div class="help-sec">
        <h3>03 // 状态释义</h3>
        <table class="help-tbl">
          <tr><td class="nw" style="width:90px"><span class="st passed"><i class="st-dot"></i><span>通过</span></span></td><td>断言全部成立，或走查完成无 ⚑ 级发现（历史恢复项带落盘时间）。</td></tr>
          <tr><td class="nw"><span class="st failed"><i class="st-dot"></i><span>失败</span></span></td><td>断言失败或 AI 走查有发现，行内展示错误摘要，点「源码/报告」看详情。</td></tr>
          <tr><td class="nw"><span class="st running"><i class="st-dot"></i><span>运行中</span></span></td><td>当前正在执行中的用例或使命卡。</td></tr>
          <tr><td class="nw"><span class="st queued"><i class="st-dot"></i><span>排队中</span></span></td><td>多选使命卡顺序执行，等待前置使命卡完成。</td></tr>
          <tr><td class="nw"><span class="st cancelled"><i class="st-dot"></i><span>已取消</span></span></td><td>用户点击「停止」或接收信号时被拦腰打断，未完整走完（非测试失败）。</td></tr>
          <tr><td class="nw"><span class="st pending"><i class="st-dot"></i><span>未运行</span></span></td><td>本轮尚未轮到执行或被跳过的项目。</td></tr>
        </table>
      </div>
      <div class="help-sec">
        <h3>04 // 可视化与录像回放</h3>
        <p><strong>显示浏览器窗口</strong>：置前 Chrome for Testing 窗口并在下方呈现实时抓帧直播，自动注入演示光标（带点击涟漪）。<br>
        <strong>录制回放视频</strong>：录制 WebM 视频，内置 10× 空闲快进、前瞻式防漏减速与步骤时间轴字幕。</p>
      </div>
    </div>
  </div>
</div>
<section id="view-trends" hidden>
  <header class="page-head"><div class="page-head-left"><h1 class="page-title">趋势</h1></div><span class="meta">只统计全量回归轮;子集/失败项重跑不入样本</span></header>
  <div class="stats-row" id="kpis"></div>
  <div class="sec" id="sec-slow" hidden>慢用例 Top<small>均值 &gt; 3s,横条越长越慢</small></div>
  <div id="slow"></div>
  <div class="sec">最近轮次</div>
  <div class="tbl-card card"><span class="tick-b"></span>
  <table class="tbl"><thead><tr><th style="width:140px">时间</th><th style="width:100px">类型</th><th>结果</th><th class="num nw" style="width:100px">耗时</th></tr></thead>
  <tbody id="hist-rows"></tbody></table></div>
</section>
</main></div><script>
const $ = id => document.getElementById(id);
const STAT = {pending:'等待',running:'运行中',passed:'通过',failed:'失败',cancelled:'取消',skipped:'跳过'};
const WHY = {pending:'还没轮到执行',running:'正在执行',passed:'断言全部通过',
  failed:'断言失败:实现有缺陷;看行内红字或「步骤」详情',
  cancelled:'停止时这条正在跑,被拦腰打断;不是测试失败',
  skipped:'本轮没执行:还没轮到就被停止,或不在选中范围'};
let busy = false;
let typeFilter = 'all', statusFilter = 'all', searchQ = '', pageNo = 1, pageSize = 10, lastMergedKeys = [];
const typeMatch = { all: () => true, det: c => c.kind === 'test' && !c.ai, ai: c => !!c.ai,
  setup: c => c.kind === 'setup', smoke: c => !!c.smoke };
// 使命卡只有 AI 试用一种类型:类型筛选为「全部/AI·模型」时显示,选确定性/前置/冒烟时不显示
const trialTypeOk = () => typeFilter === 'all' || typeFilter === 'ai';
const stateMatch = { all: () => true, passed: c => c.state === 'passed', failed: c => c.state === 'failed',
  cancelled: c => c.state === 'cancelled', skipped: c => c.state === 'skipped',
  unrun: c => c.state === 'pending' || c.state === 'running' };
// 使命卡状态词汇与用例不同,映射到同一组筛选桶(已完成≈通过,有发现≈失败,未运行≈未跑)
const trialBucket = t => t.status === 'running' ? 'unrun'
  : t.status === 'done' ? (t.exit === 0 ? 'passed' : (t.exit != null && t.exit > 128 ? 'cancelled' : 'failed'))
  : t.status === 'cancelled' ? 'cancelled' : 'unrun';
const selection = new Set();
  let lastCases = {}; let lastTrials = {};
  // 详情 Modal:点行弹出(遮罩/Esc/关闭按钮退出),顶部切换 源码/步骤/日志/回放,使命卡为报告
  const modalState = { title: '', mode: '', charter: null };
  const closeModal = () => {
    $('modal').hidden = true;
    scanToken++; activitySegs = null; stepSegs = null; scanDoneSegs = null;  // 终止后台活动扫描,清掉快进判定
    $('ff-badge').classList.remove('show'); $('vid-wrap').classList.remove('fast');
    const v = $('modal-video'); if (v) { v.pause(); v.removeAttribute('src'); v.hidden = true; }
    $('modal-pre').hidden = false; $('modal-speed').hidden = true;
    modalState.title = ''; modalState.mode = '';
  };
  $('modal-close').onclick = closeModal;
  $('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });
  $('b-help').onclick = () => { $('help-modal').hidden = false; };
  $('help-modal-close').onclick = () => { $('help-modal').hidden = true; };
  $('help-modal').addEventListener('click', e => { if (e.target === $('help-modal')) $('help-modal').hidden = true; });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (!$('help-modal').hidden) { $('help-modal').hidden = true; return; }
      if (!$('modal').hidden) closeModal();
    }
  });
  $('modal-speed').querySelectorAll('[data-rate]').forEach(b => b.onclick = () => {
    const r = +b.dataset.rate;
    $('speed').value = r; $('speed-label').textContent = r + '×';
    $('modal-video').playbackRate = r;
  });
  $('speed').oninput = e => {
    $('speed-label').textContent = (+e.target.value) + '×';
    $('modal-video').playbackRate = +e.target.value;
  };
  const fillFor = (mode, c) =>
    mode === 'code' ? async () => c.code || '(无源码)'
    : mode === 'log' ? async () => (c.log || []).join('\\n') || '(本条没有捕获到步骤输出)'
    : async () => { const r = await fetch('/artifact?p=' + encodeURIComponent(c.trace));
                    return r.ok ? await r.text() : '(这条在最近一轮没有执行记录;跑一轮后可查看步骤)'; };
  // 走查录像的步骤时间轴:分段块可点击跳转,播放中字幕显示当前步骤
  let modalSteps = [];
  // 播放头:时间轴与活动轨上一根贯穿亮线,随播放推进——进度条得让人看出"现在放到哪了"(2026-10-14 用户反馈)
  const syncPlayhead = t => {
    const v = $('modal-video'), dur = v && v.duration;
    if (!v || !dur || !isFinite(dur) || t == null) return;
    const pct = (Math.min(t, dur) / dur * 100).toFixed(2) + '%';
    for (const id of ['modal-timeline', 'modal-act']) {
      const box = $(id);
      if (!box || box.hidden) continue;
      let head = box.querySelector('.tl-head');
      if (!head) { head = document.createElement('i'); head.className = 'tl-head'; box.appendChild(head); }
      head.style.left = pct;
    }
  };
  const statusCls = s => s === 'passed' ? '' : (s === 'exhausted' || s === 'failed' ? ' bad' : ' warn');
  const renderTimeline = () => {
    const tl = $('modal-timeline'), dur = $('modal-video').duration || 0;
    if (!modalSteps.length || !dur) { tl.innerHTML = modalSteps.length ? '' : '<span class="tcap" style="margin:0">这轮走查没有留下步骤时间轴</span>'; return; }
    tl.innerHTML = modalSteps.map(s => `<div class="tl-seg${statusCls(s.status)}" data-t="${s.t}" data-i="${s.idx}"
      style="flex:${Math.max(s.dur, 0.5)}" title="第${s.idx}步(${s.t}s 起,${s.dur}s):${s.title} [${s.status}]"></div>`).join('');
    tl.querySelectorAll('.tl-seg').forEach(seg => seg.onclick = () => {
      $('modal-video').currentTime = +seg.dataset.t;
      $('modal-video').play().catch(()=>{});
    });
  };
  $('modal-video').addEventListener('loadedmetadata', renderTimeline);
  $('modal-video').addEventListener('timeupdate', () => {
    const t = $('modal-video').currentTime;
    syncPlayhead(t);
    if (!modalSteps.length) return;
    const cur = modalSteps.find(s => t >= s.t && t < s.t + s.dur);
    $('modal-caption').textContent = cur ? `第${cur.idx}步:${cur.title}(${cur.status})` : '(步骤间隙:AI 在规划下一步)';
    document.querySelectorAll('.tl-seg.cur').forEach(el => el.classList.remove('cur'));
    if (cur) document.querySelector(`.tl-seg[data-i="${cur.idx}"]`)?.classList.add('cur');
  });
  // 快进判定双来源(2026-10-14 用户反馈:只信像素扫描的话,扫描没跑完时开头空闲不跳、
  // 扫描漏判时操作段也被 10× 快进):步骤时间轴打开即用(本地 JSON 秒回),像素扫描后台补全,
  // 两者取并集——宁可少快进一段,不错快进操作段。
  // 像素扫描:隐藏 video 逐秒抽帧,8×8 分块三通道差分判定"有操作";整帧平均会把小区域
  // 变化(输入文字/光标滑过)稀释到阈值以下,分块才接得住。
  let activitySegs = null, scanToken = 0, stepSegs = null, scanDoneSegs = null;
  const mergeSegs = lists => {
    const out = [];
    for (const seg of lists.flat().filter(s => s && s[1] > s[0]).sort((x, y) => x[0] - y[0])) {
      const last = out[out.length - 1];
      if (last && seg[0] <= last[1] + 0.5) last[1] = Math.max(last[1], seg[1]);
      else out.push([seg[0], seg[1]]);
    }
    return out;
  };
  const refreshActivity = () => {
    const parts = [];
    if (scanDoneSegs) parts.push(scanDoneSegs);
    if (stepSegs) parts.push(stepSegs);
    activitySegs = parts.length ? mergeSegs(parts) : null;  // null=还没有任何依据:宁可不快进
    const dur = $('modal-video').duration;
    $('modal-act').innerHTML = (activitySegs && dur && isFinite(dur))
      ? activitySegs.map(([a, b]) =>
        `<i class="tl-bar" style="left:${(a / dur * 100).toFixed(2)}%;width:${((b - a) / dur * 100).toFixed(2)}%"></i>`).join('')
      : '';
    syncPlayhead($('modal-video').currentTime);
  };
  const scanActivity = async (src) => {
    const my = ++scanToken;
    scanDoneSegs = null;
    refreshActivity();
    // 整段下载成 blob 再扫:流式 webm 的远程 seek 每帧上百毫秒,blob 后是内存级
    let objectUrl = '';
    const v = document.createElement('video');
    try {
      const blob = await fetch(src).then(r => r.blob());
      objectUrl = URL.createObjectURL(blob);
      v.src = objectUrl; v.muted = true;
      await new Promise(res => { v.onloadedmetadata = res; v.onerror = res; });
      const dur = v.duration;
      if (!dur || !isFinite(dur) || my !== scanToken) return;
      const W = 160, H = 90, GW = 8, GH = 8;
      const c = document.createElement('canvas'); c.width = W; c.height = H;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      let prev = null;
      const marks = [];
      for (let t = 0.5; t < dur; t += 1) {  // 1s 粒度:扫描快一倍,快进判定用不着更细的分辨率
        if (my !== scanToken) return;  // Modal 已关闭或换了视频:终止本次扫描
        v.currentTime = t;
        await new Promise(r => { v.onseeked = r; });
        ctx.drawImage(v, 0, 0, W, H);
        const d = ctx.getImageData(0, 0, W, H).data;
        let active = false;
        if (prev) {
          for (let by = 0; by < GH && !active; by++) for (let bx = 0; bx < GW && !active; bx++) {
            let sum = 0, n = 0;
            const y0 = by * H / GH | 0, y1 = (by + 1) * H / GH | 0, x0 = bx * W / GW | 0, x1 = (bx + 1) * W / GW | 0;
            for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
              const p = (y * W + x) * 4;
              sum += Math.abs(d[p] - prev[p]) + Math.abs(d[p+1] - prev[p+1]) + Math.abs(d[p+2] - prev[p+2]);
              n++;
            }
            if (sum / Math.max(n, 1) / 3 > 14) active = true;  // 单块三通道均值差超阈:光标/输入/翻页都落在某几块里
          }
        }
        if (active) marks.push(t);
        prev = d.slice();
      }
      const merged = [];
      for (const t of marks) {
        const last = merged[merged.length - 1];
        if (last && t - last[1] <= 1.6) last[1] = t;
        else merged.push([t, t]);
      }
      if (my !== scanToken) return;
      scanDoneSegs = merged.map(([a, b]) => [Math.max(0, a - 0.8), Math.min(dur, b + 1.2)]);
      refreshActivity();
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    }
  };
  $('modal-video').addEventListener('timeupdate', () => {
    if (!activitySegs || !$('auto-skip').checked) { $('ff-badge').classList.remove('show'); $('vid-wrap').classList.remove('fast'); return; }
    const v = $('modal-video'), t = v.currentTime;
    const base = +$('speed').value || 1;
    // 前瞻式减速(2026-10-15 用户反馈:10× 时看不清/看漏 AI 操作):
    // timeupdate 约 0.25s 墙钟一跳,10× 下一跳媒体时间就前进 ~2.5s,
    // 反应式"进入操作段才减速"会一跳跳过整个操作段。这里按当前倍速预估
    // 下一次检查点的媒体位置,操作段起点落在 [t, 检查点] 之间就立刻降到
    // 1×——相当于提前约 1s(墙钟)开始减速,段前的光标移动与点击全程可见。
    const reach = t + Math.max(v.playbackRate, base) * 0.9 + 0.3;
    const inActive = activitySegs.some(([a, b]) => t >= a && t <= b);
    const approaching = !inActive && activitySegs.some(([a]) => a > t && a <= reach);
    const slow = inActive || approaching;
    const target = slow ? base : 10;
    if (Math.abs(v.playbackRate - target) > 0.01) v.playbackRate = target;
    // 快进状态走视频内徽章(不占步骤字幕):显示距下一个操作段的倒计时;
    // 接近操作段而提前减速的瞬间不打徽章不打扰,让操作画面干净地出现
    $('ff-badge').classList.toggle('show', !slow);
    $('vid-wrap').classList.toggle('fast', !slow);
    if (!inActive) {
      const next = activitySegs.find(([a]) => a > t);
      const rate = approaching ? base : 10;
      const remain = next ? Math.max(0, Math.round((next[0] - t) / rate)) : null;
      $('ff-countdown').textContent = remain != null ? `距下一操作约 ${remain}s` : '其后没有更多操作';
    }
  });
  const openModal = (title, mode, charter, videoPath) => {
    modalState.title = title; modalState.mode = mode; modalState.charter = charter ?? null;
    $('modal').hidden = false;
    $('modal-title').textContent = title;
    const pre = $('modal-pre'), video = $('modal-video'), speedBox = $('modal-speed'), modesBox = $('modal-modes');
    const isTrial = charter != null;
    const vpath = videoPath || (isTrial ? '' : (lastCases[title] || {}).video || '');
    speedBox.hidden = mode !== 'video';
    pre.hidden = mode === 'video';
    video.hidden = mode !== 'video';
    let fill = null;
    if (isTrial) {
      // 使命卡:报告 ↔ 录像 互切;标签随模式高亮
      modesBox.innerHTML =
        (vpath ? `<button class="dmode${mode === 'video' ? ' on' : ''}" data-m="video">回放</button>` : '')
        + (mode === 'video'
            ? '<button class="dmode" data-m="trial">报告</button>'
            : '<span class="tag ai">AI 试用官报告</span>');
      modesBox.querySelectorAll('.dmode').forEach(b => b.onclick = () =>
        openModal(title, b.dataset.m === 'video' ? 'video' : 'trial', charter, vpath));
      if (mode === 'trial') fill = async () => (await (await fetch('/trial-report?charter=' + charter)).text());
    } else {
      const c = lastCases[title] || {};
      modesBox.innerHTML = [['code','源码',true],['trace','步骤',!!c.trace],['log','日志',!!(c.log && c.log.length)],['video','回放',!!c.video]]
        .filter(m => m[2]).map(([m, label]) => `<button class="dmode${m === mode ? ' on' : ''}" data-m="${m}">${label}</button>`).join('');
      modesBox.querySelectorAll('.dmode').forEach(b => b.onclick = () => openModal(title, b.dataset.m));
      if (mode !== 'video') fill = fillFor(mode, c);
    }
    if (mode === 'video') {
      video.src = '/artifact?p=' + encodeURIComponent(vpath);
      video.playbackRate = 1; video.play().catch(()=>{});
      $('vid-wrap').hidden = false;
      $('ff-badge').classList.remove('show'); $('vid-wrap').classList.remove('fast');
      $('modal-act').hidden = false; $('auto-skip-box').hidden = false;
      scanActivity(video.src);
      // 走查录像:拉取步骤时间轴,渲染字幕轨与跳段条
      $('modal-timeline').innerHTML = ''; $('modal-caption').textContent = '';
      modalSteps = []; stepSegs = null; refreshActivity();  // 换了视频:快进判定先清零
      const showTl = isTrial && charter != null;
      $('modal-timeline').hidden = !showTl; $('modal-caption').hidden = !showTl;
      if (showTl) fetch('/trial-meta?charter=' + charter).then(r => r.json()).then(m => {
        modalSteps = m.steps || [];
        // 步骤段立即生效为快进依据:扫描还在后台跑时,开头空闲就已被跳过、操作段不会被误快进
        stepSegs = modalSteps.filter(s => s && s.t != null)
          .map(s => [Math.max(0, s.t - 0.5), s.t + Math.max(s.dur || 1, 0.5) + 0.5]);
        refreshActivity();
        renderTimeline();
      }).catch(() => { modalSteps = []; stepSegs = null; refreshActivity(); });
    } else {
      $('vid-wrap').hidden = true;
      $('ff-badge').classList.remove('show'); $('vid-wrap').classList.remove('fast');
      $('modal-timeline').hidden = true; $('modal-caption').hidden = true;
      $('modal-act').hidden = true; $('auto-skip-box').hidden = true;
      pre.textContent = '加载中…';
    }
    if (fill) Promise.resolve(fill()).then(t => { if (modalState.title === title && modalState.mode === mode) pre.textContent = t || '(无内容)'; })
      .catch(e => { pre.textContent = '加载失败:' + e; });
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
  // 统计条:主站语言的统计卡,点击联动状态筛选(active 即当前筛选);状态入口只在这里
  const cancelled = cases.filter(c=>c.state==='cancelled').length;
  const unrun = cases.filter(c=>c.state==='pending'||c.state==='running').length;
  $('stats').innerHTML = [
    ['all', cases.length, '全部', ''],
    ['passed', p, '通过', ''],
    ['failed', f, '失败', f ? 'red' : ''],
    ['skipped', skipped, '跳过', skipped ? 'dim' : ''],
    ['cancelled', cancelled, '取消', cancelled ? 'dim' : ''],
    ['unrun', unrun, '未跑', unrun ? 'dim' : ''],
  ].map(([v, n, label, cls]) =>
    `<div class="stat card${statusFilter === v ? ' active' : ''}" data-sv="${v}" title="点击按「${label}」筛选下方表格${v === 'unrun' ? '(含使命卡的未运行)' : ''}"><span class="tick-b"></span><div class="num ${cls}">${n}</div><div class="label">${label}</div></div>`).join('');
  for (const c of cases) lastCases[c.title] = c;
  for (const t of s.trials || []) lastTrials[t.id] = t;
  const fullRender = () => {
  const visible = cases.filter(c => typeMatch[typeFilter](c)
    && stateMatch[statusFilter](c) && (!searchQ || c.title.toLowerCase().includes(searchQ)));
  syncSel('gs-type', typeFilter);
  const trials = s.trials || [];
  const visibleTrials = trials.filter(t => (typeFilter === 'all' || typeFilter === 'ai')
    && (statusFilter === 'all' || trialBucket(t) === statusFilter)
    && (!searchQ || `${t.id}:${t.title}`.toLowerCase().includes(searchQ)));
  // 合并序列(用例在前、使命卡在后):分页窗口、跨页全选都作用于它
  const merged = [...visible.map(c => ({ key: c.title, c })), ...visibleTrials.map(t => ({ key: 'trial:' + t.id, t }))];
  lastMergedKeys = merged.map(m => m.key);
  const pages = Math.max(1, Math.ceil(merged.length / pageSize));
  if (pageNo > pages) pageNo = pages;
  const caseRow = (c, num) => {
    const isChecked = selection.has(c.title);
    return `<tr data-title="${c.title.replace(/"/g,'&quot;')}" class="${isChecked?'row-selected':''}">
      <td style="text-align:center"><input type="checkbox" data-t="${c.title.replace(/"/g,'&quot;')}" ${isChecked?'checked':''}></td>
      <td class="num">${num}</td>
      <td>
        <div class="case-title">${c.title}</div>
        ${c.note ? `<div class="note${c.state==='cancelled'?' cancelled':''}"><span class="note-tag">${c.state==='cancelled'?'STOP':'ERR'}</span><span>${c.note}</span></div>` : ''}
        ${c.info ? `<div class="info"><span class="info-tag">INFO</span><span>${c.info}</span></div>` : ''}
      </td>
      <td class="nw">${c.kind==='setup'?'<span class="tag det">前置</span>':c.ai?'<span class="tag ai">AI·模型</span>':'<span class="tag det">确定性</span>'}${c.smoke?'<span class="tag smoke" title="冒烟子集:check.py smoke 快速门禁只跑带此标的用例,全量回归则都跑">冒烟</span>':''}</td>
      <td class="nw"><span class="st ${c.state}" title="${WHY[c.state]||''}"><i class="st-dot"></i><span>${STAT[c.state]||c.state}</span></span></td>
      <td class="num">${c.durationMs!=null?(c.durationMs/1000).toFixed(1)+'s':'—'}</td>
      <td class="nw row-actions">
        <button class="btn-row row-run" data-title="${c.title.replace(/"/g,'&quot;')}" title="只运行这一条(所需前置自动附带)" ${busy?'disabled':''}>▶ 运行</button>
        <button class="btn-row row-view" data-title="${c.title.replace(/"/g,'&quot;')}" data-mode="code" title="查看这条的测试源码">源码</button>
        ${c.video ? `<button class="btn-row row-view view-vid" data-title="${c.title.replace(/"/g,'&quot;')}" data-mode="video" title="观看这条的回放录像">回放</button>` : ''}
      </td>
    </tr>`;
  };
  const offset = visible.length;  // 使命卡编号紧接用例编号(67 起),不用"试用N"特殊名
  const trialRow = t => {
      // 历史恢复的结果(驾驶舱重启前跑的)带 restored+ran_at:显示「上次…· 时间」并说明来路,不冒充本轮「已完成」
      const ran = t.restored ? ` · ${t.ran_at}` : '';
      const histTip = t.restored ? `驾驶舱重启前的历史走查结果(${t.ran_at} 报告落盘),本轮未运行;` : '';
      const st = t.status==='running' ? '<span class="st running" title="AI 正在真实浏览器里走查,数分钟"><i class="st-dot"></i><span>运行中</span></span>'
        : t.status==='queued' ? '<span class="st queued" title="顺序执行:排在前面的使命卡跑完后自动开始这张"><i class="st-dot"></i><span>排队中</span></span>'
        : t.status==='done' ? (t.exit===0 ? `<span class="st passed" title="${histTip}走查完成,没有 ⚑ 级发现"><i class="st-dot"></i><span>${t.restored?'上次通过':'已完成'}${ran}</span></span>`
            : (t.exit!=null && t.exit>128) ? `<span class="st cancelled" title="${histTip}退出码>128=被信号终止(如停止指令),未走查完"><i class="st-dot"></i><span>${t.restored?'上次取消':'已取消'}${ran}</span></span>`
            : `<span class="st failed" title="${histTip}AI 走查发现 ⚑ 级问题;点本行「报告」查看具体发现与证据"><i class="st-dot"></i><span>${t.restored?'上次有发现':'有发现'}${ran}</span></span>`)
        : t.status==='cancelled' ? '<span class="st cancelled" title="被停止指令/信号打断,未走查完"><i class="st-dot"></i><span>已取消</span></span>'
        : '<span class="st pending" title="这张卡还没跑过"><i class="st-dot"></i><span>未运行</span></span>';
      const isChecked = selection.has('trial:' + t.id);
      return `<tr data-trial="${t.id}" class="${isChecked?'row-selected':''}">
        <td style="text-align:center"><input type="checkbox" data-t="trial:${t.id}" ${isChecked?'checked':''}></td>
        <td class="num">${offset + t.id}</td>
        <td><div class="case-title">使命 ${t.id}:${t.title}</div></td>
        <td class="nw"><span class="tag trial">AI 试用</span></td>
        <td class="nw">${st}</td>
        <td class="num">${t.duration_s!=null?t.duration_s+'s':'—'}</td>
        <td class="nw row-actions">
          <button class="btn-row watch trial-run" data-charter="${t.id}" ${t.status==='running'||busy?'disabled':''}>${t.status==='running'?'运行中…':'▶ 运行'}</button>
          <button class="btn-row row-view view-trial" data-charter="${t.id}" data-mode="trial" title="查看这张卡的走查报告(含录像入口)">报告</button>
          ${t.video ? `<button class="btn-row row-view view-vid" data-charter="${t.id}" data-mode="video" title="观看这张卡的走查录像">回放</button>` : ''}
        </td>
      </tr>`;
  };
  const start = (pageNo - 1) * pageSize;
  const slice = merged.slice(start, start + pageSize);
  let num = start, html = '';
  for (const m of slice) { if (m.c) { num++; html += caseRow(m.c, num); } else { html += trialRow(m.t); } }
  $('rows').innerHTML = html || '<tr><td colspan="7" class="dim" style="text-align:center;padding:24px">没有匹配的行:换个筛选条件或清空搜索词</td></tr>';
  // 分页条:同主站 ListPager——单页时整条不渲染(数量由顶部统计行承担)
  const pbar = $('pbar');
  if (pages <= 1) { pbar.style.display = 'none'; }
  else {
    pbar.style.display = 'flex';
    $('p-total').textContent = `共 ${merged.length} 条`;
    syncSel('gs-size', String(pageSize));
    let nav = '';
    if (pageNo > 1) nav += `<button class="pgbtn" data-pg="${pageNo - 1}">上一页</button>`;
    nav += Array.from({ length: pages }, (_, i) => `<button class="pgbtn${i + 1 === pageNo ? ' on' : ''}" data-pg="${i + 1}">${i + 1}</button>`).join('');
    if (pageNo < pages) nav += `<button class="pgbtn" data-pg="${pageNo + 1}">下一页</button>`;
    $('p-pages').innerHTML = nav;
  }
  const fc = $('filter-count');
  if (fc) fc.textContent = `共 ${merged.length} 项`;
  };
  // 详情在 Modal 里,不依附表格行:整表可随时安全重绘
  fullRender();
  document.querySelectorAll('#rows input[type=checkbox]').forEach(cb => cb.onchange = () => {
    const tr = cb.closest('tr');
    if (cb.checked) {
      selection.add(cb.dataset.t);
      if (tr) tr.classList.add('row-selected');
    } else {
      selection.delete(cb.dataset.t);
      if (tr) tr.classList.remove('row-selected');
    }
    $('b-selected').textContent = `▶ 运行选中${selection.size?` (${selection.size})`:''}`;
    $('b-selected').disabled = busy || selection.size === 0;
  });
  // 表头全选框状态跟随「筛选全集」而非当前页(跨页全选)
  const picked = lastMergedKeys.filter(k => selection.has(k)).length;
  $('c-all').checked = lastMergedKeys.length > 0 && picked === lastMergedKeys.length;
  $('c-all').indeterminate = picked > 0 && picked < lastMergedKeys.length;
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
  // 页面标题随进度更新(浏览器标签页上一眼可见本轮结果)
  const done = cases.filter(c => c.state !== 'pending' && c.state !== 'running');
  document.title=`${done.length}/${total} ${f?'×'+f:'✓'} · Gitwire 回归驾驶舱`;
});}
// 行内交互用事件委托:表格每 600ms 重渲染,逐节点绑定会命中被换掉的旧节点
$('rows').addEventListener('click', event => {
  const run = event.target.closest('.trial-run');
  if (run) {
    run.disabled = true; run.textContent = '运行中…';
    fetch('/api/control',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({action:'trial',charter:+run.dataset.charter, headed:$('c-headed').checked})});
    return;
  }
  // 用例操作列:只跑这一条(复用「运行选中」的路径,所需前置自动附带)
  const one = event.target.closest('.row-run');
  if (one) {
    ctl('start', {selected:[one.dataset.title], headed:$('c-headed').checked, video:$('c-video').checked});
    return;
  }
  // 操作列的查看按钮:源码/报告/回放 直达 Modal
  const peek = event.target.closest('.row-view');
  if (peek) {
    if (peek.dataset.mode === 'trial') openModal(`使命 ${peek.dataset.charter} · 试用报告`, 'trial', +peek.dataset.charter, (lastTrials[+peek.dataset.charter] || {}).video);
    else if (peek.dataset.mode === 'video') {
      if (peek.dataset.charter) openModal(`使命 ${peek.dataset.charter} · 试用报告`, 'video', +peek.dataset.charter, (lastTrials[+peek.dataset.charter] || {}).video);
      else openModal(peek.dataset.title, 'video');
    }
    else openModal(peek.dataset.title, 'code');
    return;
  }
});
// 单选筛选用主题化下拉(同主站 GSelect 的交互:点开浮层单选,点外部收起)
const syncSel = (id, value) => {
  const box = $(id);
  box.querySelectorAll('.gsel-opt').forEach(o => o.classList.toggle('on', o.dataset.v === value));
  const current = box.querySelector('.gsel-opt.on');
  box.querySelector('.gsel-btn .val').textContent = current ? current.textContent : value;
};
const initSel = (id, apply) => {
  const box = $(id);
  const list = box.querySelector('.gsel-list');
  box.querySelector('.gsel-btn').onclick = e => {
    e.stopPropagation();
    document.querySelectorAll('.gsel-list').forEach(l => { if (l !== list) l.hidden = true; });
    list.hidden = !list.hidden;
  };
  box.querySelectorAll('.gsel-opt').forEach(o => o.onclick = e => {
    e.stopPropagation();
    list.hidden = true;
    apply(o.dataset.v);
  });
};
document.addEventListener('click', () => document.querySelectorAll('.gsel-list').forEach(l => l.hidden = true));
initSel('gs-type', v => { typeFilter = v; pageNo = 1; tick(); });
initSel('gs-size', v => { pageSize = +v; pageNo = 1; tick(); });
$('f-clear').onclick = () => {
  $('f-search').value = '';
  searchQ = '';
  $('f-clear').hidden = true;
  pageNo = 1;
  tick();
};
$('f-search').oninput = e => {
  searchQ = e.target.value.trim().toLowerCase();
  $('f-clear').hidden = !searchQ;
  pageNo = 1;
  tick();
};
$('p-pages').onclick = e => { const b = e.target.closest('[data-pg]'); if (b) { pageNo = +b.dataset.pg; tick(); } };
// 统计卡:点击即按该状态筛选表格(同主站统计卡即分组 tab 的交互)
$('stats').onclick = e => { const c = e.target.closest('[data-sv]'); if (c) { statusFilter = c.dataset.sv; pageNo = 1; tick(); } };
// 一键全选:作用于当前筛选全集(跨页,含未显示到本页的行);不在筛选内的行选中态保持原样
$('c-all').onchange = () => {
  const on = $('c-all').checked;
  lastMergedKeys.forEach(k => { on ? selection.add(k) : selection.delete(k); });
  document.querySelectorAll('#rows tr').forEach(tr => {
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) {
      cb.checked = on;
      tr.classList.toggle('row-selected', on);
    }
  });
  $('b-selected').textContent = `▶ 运行选中${selection.size?` (${selection.size})`:''}`;
  $('b-selected').disabled = busy || selection.size === 0;
};
setInterval(tick,600); tick();
$('b-start').onclick=()=>ctl('start'); $('b-stop').onclick=()=>ctl('stop');
$('b-failed').onclick=()=>ctl('last-failed');
$('b-selected').onclick=()=>{
  const titles=[...selection].filter(k=>!k.startsWith('trial:'));
  const trials=[...selection].filter(k=>k.startsWith('trial:')).map(k=>+k.slice(6));
  ctl('start', {selected:titles.length?titles:null, trials:trials.length?trials:null,
    headed:$('c-headed').checked, video:$('c-video').checked});
};
$('b-start').onclick=()=>ctl('start', {headed:$('c-headed').checked, video:$('c-video').checked});
// ── 趋势页:统计看板(进入页面时拉取 /history 渲染) ──
const loadTrends = async () => {
  try {
    const h = await (await fetch('/history')).json();
    const kpi = (val, label, cls) => `<div class="stat card"><span class="tick-b"></span><div class="num ${cls || ''}">${val}</div><div class="label">${label}</div></div>`;
    $('kpis').innerHTML =
      kpi(h.full_runs ?? 0, '全量回归轮') +
      kpi(h.pass_rate != null ? (h.pass_rate * 100).toFixed(0) + '%' : '—', '全量轮成功率', (h.pass_rate ?? 1) < 0.9 ? 'amber' : '') +
      kpi(h.avg_duration_s != null ? h.avg_duration_s + 's' : '—', '全量轮平均耗时') +
      kpi((h.recent || [])[0]?.duration_s != null ? (h.recent[0].duration_s + 's') : '—', '最近一轮耗时');
    const slow = h.slow_top || [];
    $('sec-slow').hidden = !slow.length;
    const worst = Math.max(...slow.map(c => c.avg_s), 0.001);
    $('slow').innerHTML = slow.map(c =>
      `<div class="slowrow"><span class="name" title="${c.title.replace(/"/g,'&quot;')}">${c.title}</span>
       <span class="track"><i style="width:${(c.avg_s / worst * 100).toFixed(1)}%"></i></span>
       <span class="num">${c.avg_s}s · ${c.runs}次</span></div>`).join('');
    const rows = (h.recent || []).map(e => {
      const m = /^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}:\\d{2})/.exec(e.at || '');
      return `<tr><td class="num">${m ? (m[2] + '-' + m[3] + ' ' + m[4]) : (e.at || '')}</td><td>${{full:'全量',subset:'子集','last-failed':'失败项'}[e.kind] || e.kind}</td>
       <td class="st ${e.failed ? 'failed' : 'passed'}">${e.failed ? ('失败 ' + e.failed) : '通过'}${e.passed != null ? (' · ' + e.passed + ' 过') : ''}</td>
       <td class="num">${e.duration_s ?? ''}s</td></tr>`;}).join('');
    $('hist-rows').innerHTML = rows || '<tr><td colspan="4" class="dim">还没有历史,跑一轮全量即有</td></tr>';
  } catch (e) { $('kpis').innerHTML = `<p class="meta">加载失败:${e}</p>`; }
};
// hash 路由:左侧导航在「回归 / 趋势」两个视图间切换
const route = () => {
  // 默认落地 01 号导航项「趋势」(同主站 / → 01 号页的约定)
  const page = location.hash === '#/run' ? 'run' : 'trends';
  $('view-run').hidden = page !== 'run';
  $('view-trends').hidden = page !== 'trends';
  document.querySelectorAll('.nav').forEach(a => a.classList.toggle('on', a.hash === (page === 'run' ? '#/run' : '#/trends')));
  if (page === 'trends') loadTrends();
};
window.addEventListener('hashchange', route);
route();
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
            # 两个白名单根:用例工件相对 results/(一轮一清);使命卡录像相对仓库根(持久在 data/trial-videos/)
            target = None
            if rel:
                results_root = (FRONTEND / '.e2e' / 'results').resolve()
                candidate = (results_root / rel).resolve()
                if candidate.is_relative_to(results_root) and candidate.is_file():
                    target = candidate
                else:
                    candidate = (ROOT / rel).resolve()
                    if candidate.is_relative_to((ROOT / 'data' / 'trial-videos').resolve()) and candidate.is_file():
                        target = candidate
            if target is None:
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
        elif self.path.startswith('/trial-meta'):
            from urllib.parse import urlparse, parse_qs
            number = (parse_qs(urlparse(self.path).query).get('charter') or [''])[0]
            meta = ROOT / 'data' / 'trial-videos' / f'mission-{number}.meta.json'
            self._send(200, (meta.read_text() if meta.exists() else '{"steps":[]}').encode(),
                       'application/json')
        elif self.path.startswith('/trial-report'):
            from urllib.parse import urlparse, parse_qs, unquote
            report_path = ROOT / 'data' / 'ai-trial-report.md'
            text = report_path.read_text() if report_path.exists() else '(还没有试用报告;跑一张使命卡即生成)'
            number = (parse_qs(urlparse(self.path).query).get('charter') or [''])[0]
            if number:
                sections = re.split(r'(?=^## 使命 )', text, flags=re.M)
                section = next((x for x in sections if x.startswith(f'## 使命 {number}:')), '')
                text = section or f'(使命 {number} 还没有跑过;点行内「▶ 运行」生成)'
            self._send(200, text.encode())
        elif self.path.startswith('/trial'):
            sys.path.insert(0, str(ROOT / 'scripts'))
            from ai_trial import CHARTERS
            self._send(200, json.dumps(
                [{'id': c['id'], 'title': c['title'], 'session': c['session'] or '访客'} for c in CHARTERS],
                ensure_ascii=False).encode(), 'application/json')
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
                sys.path.insert(0, str(ROOT / 'scripts'))
                from ai_trial import CHARTERS as _CHARTERS
                trials = [{'id': c['id'], 'title': c['title'], 'session': c['session'] or '访客',
                           **state['trials'].get(c['id'], {'status': 'idle', 'exit': None, 'duration_s': None})}
                          for c in _CHARTERS]
                snapshot = json.dumps({'status': state['status'], 'total': state['total'], 'headed': state.get('headed'),
                                       'trials': trials,
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
                              headed=bool(payload.get('headed')), video=bool(payload.get('video')),
                              trial_ids=payload.get('trials') or None)
            elif action == 'trial':
                charter = int(payload.get('charter') or 0)
                trial_headed = bool(payload.get('headed'))  # 行内「▶ 运行」带上「显示浏览器窗口」勾选状态
                if state['status'] not in ('running', 'starting'):
                    threading.Thread(target=run_single_trial, args=(charter, trial_headed), daemon=True).start()
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
