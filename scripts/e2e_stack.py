#!/usr/bin/env python3
"""Isolated browser-e2e stack: sqlite backend (10248) + built-frontend preview (10249) + seed data.

Only manages the ports registered as e2e_api/e2e_web in ~/.config/local-deploy/ports.yaml.
Never touches the shared acceptance services (10240/10241), Postgres/Redis volumes,
or any real account data — the database is a throwaway sqlite file under data/e2e-stack/.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
BASE = ROOT / 'data' / 'e2e-stack'
DB = BASE / 'e2e.db'
RUNTIME_ENV = BASE / 'runtime.env'
STACK = BASE / 'stack.json'
API_LOG = BASE / 'api.log'
WEB_LOG = BASE / 'web.log'
API_PORT, WEB_PORT = 10248, 10249
API = f'http://127.0.0.1:{API_PORT}'
WEB = f'http://127.0.0.1:{WEB_PORT}'
SESSION_TOKEN = 'e2e-pilot-session'
SESSION_DAYS = 1

# Seed contract mirrored by frontend/e2e/*.e2e.ts — change both together.
SEED = {
    'repos': ['pilot/alpha', 'pilot/beta'],
    'issues': [
        # (repo, number, title, title_zh, state, status, difficulty, age_hours, comments)
        ('pilot/alpha', 11, 'Fix login race condition', '修复登录竞态条件', 'open', 'open', '简单', 1, 1),
        ('pilot/alpha', 7, 'Add pagination to archive list', '', 'open', 'open', '简单', 2, 0),
        ('pilot/alpha', 9, 'Hot discussion: rewrite config', '', 'open', 'open', '简单', 3, 99),  # 评论过热 → 未入榜
        ('pilot/beta', 3, 'Docs: fix typo in README', '', 'closed', 'closed', '简单', 24 * 3, 0),
        ('pilot/beta', 5, 'Investigate flaky parser test', '', 'open', 'open', '未分析', 24 * 5, 0),
    ],
    # 候选仓库:starred 源 29 条(28 条带 Star 时间按小时递减 = star-01 最新;
    # star-00 无 Star 时间排最后;star-05 即 pilot/alpha 已监控),ignored 2 条,owned 3 条
    'starred_count': 28,
    'owned': ['pilot/owned-a', 'pilot/owned-b', 'pilot/owned-c'],
    'ignored': ['pilot/old-idea-1', 'pilot/old-idea-2'],
}


def run(*args, check=True, capture=True, env=None, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          env=env,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def listeners(port: int) -> list[str]:
    return run('lsof', f'-tiTCP:{port}', '-sTCP:LISTEN', check=False).stdout.split()


def http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def ensure_runtime_env() -> dict[str, str]:
    BASE.mkdir(parents=True, exist_ok=True)
    if not RUNTIME_ENV.exists():
        from cryptography.fernet import Fernet

        RUNTIME_ENV.write_text('ENCRYPTION_KEY=' + Fernet.generate_key().decode() + '\n')
        RUNTIME_ENV.chmod(0o600)
    values = {}
    for line in RUNTIME_ENV.read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value
    return values


def api_env() -> dict[str, str]:
    values = ensure_runtime_env()
    return {
        **os.environ,
        # 覆盖 .env 中的共享设施指向，保证与真实数据完全隔离
        'HOSTED_ENABLED': 'true',
        'DATABASE_URL': f'sqlite:///{DB}',
        'REDIS_URL': '',
        'ENCRYPTION_KEY': values['ENCRYPTION_KEY'],
        'PUBLIC_URL': WEB,
        'GITHUB_CLIENT_ID': 'e2e-local',
        'GITHUB_CLIENT_SECRET': 'e2e-local-secret',
        'DATA_DIR': str(BASE),
        'GITWIRE_VAULT': str(BASE / 'vault'),
        'SECRET_KEY': '',
        'BARK_URL': '',
        'SMTP_HOST': '',
        'LLM_BASE_URL': '',
        'LLM_MODEL': 'unused-e2e',
    }


def seed() -> None:
    """Recreate the throwaway sqlite DB with the pilot account and deterministic data."""
    from sqlmodel import SQLModel, Session

    from app.config import Settings
    from app.hosted.models import (Account, Artifact, BrowserSession, Candidate, Delivery, Digest,
                                   Event, Issue, Job, JobAccess, LegacyRecord, Notification,
                                   Repository, Subscription, now)
    from app.hosted.security import digest
    from app.hosted.store import engine_for

    cfg = Settings(_env_file=None, hosted_enabled=True, database_url=f'sqlite:///{DB}',
                   encryption_key=ensure_runtime_env()['ENCRYPTION_KEY'], public_url=WEB,
                   github_client_id='e2e-local', github_client_secret='e2e-local-secret',
                   data_dir=str(BASE))
    engine = engine_for(cfg)
    # drop+create 而非删文件:栈复用时 uvicorn 仍持有连接,同名文件内重建最安全
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    at = now()
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    with Session(engine) as s:
        s.add(Account(id='pilot', github_id='9001', login='pilot'))
        s.add(BrowserSession(digest=digest(SESSION_TOKEN), user_id='pilot', csrf='e2e-pilot-csrf',
                             expires_at=at + SESSION_DAYS * 86400))
        s.commit()
        repos = {}
        for index, full_name in enumerate(SEED['repos']):
            repo = Repository(id=f'repo-{index + 1}', github_id=str(90001 + index), full_name=full_name,
                              description=f'e2e seed repo {full_name}',
                              pushed_at=at, external_merge_at=at, signals_checked_at=at,
                              signals_coverage_since=1, checked_at=at)  # checked_at 新鲜,/explore 直接命中缓存
            s.add(repo)
            repos[full_name] = repo
            s.add(Subscription(id=f'sub-{index + 1}', user_id='pilot', repo_id=repo.id))
        s.commit()
        for repo_name, number, title, title_zh, state, status, difficulty, age_hours, comments in SEED['issues']:
            created = at - age_hours * 3600
            s.add(Issue(id=f'issue-{number}', repo_id=repos[repo_name].id, number=number, title=title,
                        title_zh=title_zh, title_zh_source=title if title_zh else '',
                        url=f'https://github.com/{repo_name}/issues/{number}',
                        created_at=iso(created), updated_at='', state=state, status=status,
                        difficulty=difficulty, comments=comments))
        s.commit()
        # 契约细化:#7 挂一条"原文已变"的过期翻译(界面必须隐藏);#11 带完整分析;
        # pilot 对 alpha 的订阅追踪 #3(追踪中分组)
        seven = s.get(Issue, 'issue-7')
        seven.title_zh, seven.title_zh_source = '旧版翻译(不应显示)', 'Add pagination to archive list (旧标题)'
        s.add(seven)
        eleven = s.get(Issue, 'issue-11')
        eleven.summary = '登录流程存在并发窗口,两个请求可同时进入临界区'
        eleven.problem = '**竞态**:`login()` 检查与写入之间没有互斥,可重复建立会话。'
        eleven.plan = '- 为会话建立加互斥锁\n- 写入后二次校验会话唯一性'
        eleven.evidence = [{'reason': '存在明确声明修复本 Issue 的开放 PR', 'kind': 'pr', 'repo': 'pilot/alpha',
                            'number': 20, 'open': True, 'merged': False, 'author': 'ext',
                            'url': 'https://github.com/pilot/alpha/pull/20'}]
        eleven.analyzed_at = at
        eleven.body = '## 复现步骤\n\n1. 并发登录两个会话\n2. 观察会话表出现重复行'
        s.add(eleven)
        beta_sub = s.get(Subscription, 'sub-2')  # #3 属于 beta,追踪过滤按 (repo, number) 联合生效
        beta_sub.watched_issues = [3]
        s.add(beta_sub)
        s.commit()
        # ── 候选仓库:我的 Star(29 条,star-01 最新,star-00 无时间排最后)──
        for index in range(SEED['starred_count']):
            name = f'pilot/star-{index + 1:02d}'
            monitored = index == 4  # star-05 = pilot/alpha,github_id 对上已订阅仓库
            s.add(Candidate(id=f'cand-star-{index + 1}', user_id='pilot',
                            github_id='90001' if monitored else str(91000 + index),
                            full_name='pilot/alpha' if monitored else name,
                            source='starred', starred_at=iso(at - (index + 1) * 3600)))
        s.add(Candidate(id='cand-star-00', user_id='pilot', github_id='91999',
                        full_name='pilot/star-00', source='starred', starred_at=''))
        for index, name in enumerate(SEED['ignored']):
            s.add(Candidate(id=f'cand-ignored-{index}', user_id='pilot', github_id=str(93000 + index),
                            full_name=name, source='starred', starred_at=iso(at - 90000 - index * 3600),
                            ignored=True))
        for index, name in enumerate(SEED['owned']):
            s.add(Candidate(id=f'cand-owned-{index}', user_id='pilot', github_id=str(94000 + index),
                            full_name=name, source='owned'))
        s.commit()
        # ── 运行历史:三种任务类型/状态 ──
        jobs = [
            ('job-scan', 'scan', 'completed', 1, {'count': 5}, ''),
            ('job-archive', 'analyze_repo', 'failed', 3, {}, '连接超时'),
            ('job-digest', 'digest', 'pending', 2, {}, ''),
        ]
        for job_id, kind, status, attempts, result, error in jobs:
            s.add(Job(id=job_id, kind=kind, payload={'repo_id': 'repo-1'}, user_id='pilot',
                      status=status, attempts=attempts, result=result, error=error,
                      created_at=at - 7200, available_at=at))
            s.add(JobAccess(job_id=job_id, user_id='pilot'))
        s.commit()
        # ── 警报(一条已读、一条未读)──
        events = [('event-opportunity', 'opportunity', '发现可上手 Issue', '#11 可上手:修复登录竞态条件', 'repo-1'),
                  ('event-release', 'release', 'pilot/beta 发布 v2.0', '新版本包含破坏性变更', 'repo-2')]
        for event_id, kind, title, body, repo_id in events:
            s.add(Event(id=event_id, key=event_id, repo_id=repo_id, kind=kind, title=title, body=body,
                        url=f'https://github.com/x/{event_id}'))
        s.commit()
        s.add(Notification(id='notice-read', user_id='pilot', event_id='event-opportunity', read_at=at - 100))
        s.add(Notification(id='notice-unread', user_id='pilot', event_id='event-release'))
        s.commit()
        # ── 每日晨报 + 项目档案(两个版本)+ 投递记录 + 旧版资料 ──
        s.add(Digest(id='digest-today', user_id='pilot',
                     date=datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%d'),
                     content='# 每日晨报\n\n- pilot/alpha:发现可上手的 Issue #11\n- pilot/beta:发布 v2.0,注意破坏性变更'))
        s.add(Artifact(id='art-r1', repo_id='repo-1', path='docs/overview.md', revision='r1', content='# v1 概览'))
        s.add(Artifact(id='art-r2', repo_id='repo-1', path='docs/overview.md', revision='r2', content='# v2 概览\n\n新增认证模块说明。'))
        s.add(Delivery(id='delivery-1', user_id='pilot', key='delivery-1', recipient='pilot@example.com',
                       subject='Gitwire 警报:发现可上手 Issue', body='Issue #11 可上手', status='sent',
                       created_at=at - 3600, sent_at=at - 3500))
        s.add(Delivery(id='delivery-2', user_id='pilot', key='delivery-2', recipient='pilot@example.com',
                       subject='Gitwire 每日晨报', body='今日动态汇总', status='failed', created_at=at - 7200,
                       error='SMTP 550: mailbox unavailable'))
        s.add(LegacyRecord(id='legacy-1', user_id='pilot', source_key='legacy-1', kind='document',
                           repo='pilot/alpha', title='旧版项目档案:pilot/alpha', content='# 旧版档案内容',
                           created_at=at - 30 * 86400))
        s.commit()
        # ── 第二用户 writer:写操作测试专用(独立仓库/警报,与 pilot 的只读断言互不干扰)──
        s.add(Account(id='writer', github_id='9002', login='writer'))
        s.add(BrowserSession(digest=digest('e2e-writer-session'), user_id='writer', csrf='e2e-writer-csrf',
                             expires_at=at + SESSION_DAYS * 86400))
        s.commit()
        gamma = Repository(id='repo-3', github_id='90003', full_name='pilot/gamma',
                           description='e2e seed repo pilot/gamma', pushed_at=at, external_merge_at=at,
                           signals_checked_at=at, signals_coverage_since=1, checked_at=at)
        s.add(gamma)
        s.add(Subscription(id='sub-3', user_id='writer', repo_id=gamma.id))
        s.commit()
        s.add(Issue(id='issue-21', repo_id=gamma.id, number=21, title='Gamma edge case', state='open',
                    status='open', difficulty='简单', comments=0, url='https://github.com/pilot/gamma/issues/21',
                    created_at=iso(at - 1800), updated_at=''))
        s.add(Event(id='event-writer', key='event-writer', repo_id=gamma.id, kind='opportunity',
                    title='writer 的警报', body='gamma 出现机会', url='https://github.com/x/event-writer'))
        s.commit()
        s.add(Notification(id='notice-writer', user_id='writer', event_id='event-writer'))
        s.commit()
        # ── 第三账户 quitter:专供"退出登录"测试(登出会删服务端会话,
        #    用独立账户避免并行打掉 pilot/writer 的登录态)──
        s.add(Account(id='quitter', github_id='9003', login='quitter'))
        s.add(BrowserSession(digest=digest('e2e-quitter-session'), user_id='quitter',
                             csrf='e2e-quitter-csrf', expires_at=at + SESSION_DAYS * 86400))
        s.commit()
    engine.dispose()
    print(f'seeded {DB.name}: {len(SEED["repos"])} repos / {len(SEED["issues"])} issues / account pilot')


def spawn(name: str, args: list[str], env: dict[str, str], log: Path) -> int:
    log_handle = log.open('ab')
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
                            start_new_session=True)
    STACK.parent.mkdir(parents=True, exist_ok=True)
    stack = json.loads(STACK.read_text()) if STACK.exists() else {}
    stack[name] = proc.pid
    STACK.write_text(json.dumps(stack, indent=2))
    return proc.pid


def up() -> None:
    if STACK.exists():
        state = json.loads(STACK.read_text())
        alive = [pid for pid in state.values() if _pid_alive(pid)]
        if alive and all(alive) and http_ok(f'{API}/api/health') and http_ok(f'{WEB}/'):
            seed()  # 复用进程,但每次都要全新的种子世界,保证测试确定性
            print(f'e2e stack already up: api {API} · web {WEB} (reused pids {alive}, reseeded)')
            return
        STACK.unlink(missing_ok=True)  # 残留记录(进程已死或端口未就绪),清掉重起
    for port in (API_PORT, WEB_PORT):
        if listeners(port):
            raise SystemExit(f'Port {port} is occupied by another process; refusing to start the e2e stack')
    if not (FRONTEND / 'dist' / 'index.html').exists():
        raise SystemExit('frontend/dist is missing; run `npm --prefix frontend run build` first')
    seed()
    api = api_env()
    web = {**os.environ, 'E2E_API_TARGET': API}
    pid_api = spawn('api', ['uv', 'run', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                            '--port', str(API_PORT), '--log-level', 'warning'], api, API_LOG)
    pid_web = spawn('web', ['npm', '--prefix', 'frontend', 'run', 'preview', '--',
                            '--port', str(WEB_PORT), '--strictPort', '--host', '127.0.0.1'], web, WEB_LOG)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if http_ok(f'{API}/api/health') and http_ok(f'{WEB}/'):
            print(f'e2e stack up: api {API} (pid {pid_api}) · web {WEB} (pid {pid_web})')
            print(f'logs: {API_LOG} · {WEB_LOG}')
            return
        if not _pid_alive(pid_api) or not _pid_alive(pid_web):
            print(f'--- process died; tail {API_LOG} ---', API_LOG.read_text()[-2000:], sep='\n')
            print(f'--- tail {WEB_LOG} ---', WEB_LOG.read_text()[-2000:], sep='\n')
            down()
            raise SystemExit('e2e stack failed to become healthy')
        time.sleep(0.5)
    down()
    raise SystemExit('e2e stack did not become healthy within 90s')


def _pid_alive(pid: int) -> bool:
    return run('kill', '-0', str(pid), check=False).returncode == 0


def down() -> None:
    if not STACK.exists():
        print('e2e stack is not running')
        return
    state = json.loads(STACK.read_text())
    for name, pid in state.items():
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in state.values()):
        time.sleep(0.25)
    for pid in state.values():
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    STACK.unlink(missing_ok=True)
    left = [p for p in (API_PORT, WEB_PORT) if listeners(p)]
    print('e2e stack down', f'(ports still listening: {left})' if left else '')


def status() -> bool:
    running = False
    if STACK.exists():
        state = json.loads(STACK.read_text())
        alive = {name: _pid_alive(pid) for name, pid in state.items()}
        running = all(alive.values())
        print('processes', alive)
    print('api', API, 'healthy' if http_ok(f'{API}/api/health') else 'down')
    print('web', WEB, 'healthy' if http_ok(f'{WEB}/') else 'down')
    print('db', DB, 'exists' if DB.exists() else 'missing')
    return running


def watch_frontmost(stop) -> None:
    """macOS 后台进程开的窗口不会自动置前;headed 演示时每秒把测试浏览器推到最前。"""
    while not stop.is_set():
        subprocess.run(['osascript', '-e',
                        'tell application "System Events" to set frontmost of '
                        '(first application process whose name contains "Chrome for Testing") to true'],
                       capture_output=True)
        stop.wait(1.2)


def run_e2e(keep: bool, extra: list[str]) -> int:
    import threading

    try:
        up()
        stop = threading.Event()
        watcher = None
        if '--headed' in extra and sys.platform == 'darwin':
            watcher = threading.Thread(target=watch_frontmost, args=(stop,), daemon=True)
            watcher.start()
        try:
            return subprocess.run(['npm', 'run', 'e2e', '--', *extra], cwd=FRONTEND).returncode
        finally:
            stop.set()
            if watcher:
                watcher.join(timeout=2)
    finally:
        if not keep:
            down()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['up', 'down', 'status', 'run', 'seed'])
    parser.add_argument('--keep', action='store_true', help='run: keep the stack up after tests')
    parser.add_argument('rest', nargs='*', help='run: extra args forwarded to `e2e run` (e.g. --headed)')
    args = parser.parse_args()
    if args.action == 'seed':
        seed()
    elif args.action == 'up':
        up()
    elif args.action == 'down':
        down()
    elif args.action == 'status':
        sys.exit(0 if status() else 1)
    elif args.action == 'run':
        sys.exit(run_e2e(args.keep, args.rest))


if __name__ == '__main__':
    main()
