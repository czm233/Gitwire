"""CZM-34 V04 验收就绪环境：为本地 sqlite 库写入演示数据（幂等，重复执行先清后写）。

用法：uv run python scripts/seed_v04_acceptance.py
输出：浏览器会话 token（设置 cookie gitwire_session=<token> 即为登录态）。
仅用于 macmini 就地验收，不进生产、不推送。
"""
from datetime import datetime, timedelta, timezone

from sqlmodel import SQLModel, Session, create_engine, delete
from app.config import Settings
from app.hosted import models as m
from app.hosted.security import digest

SESSION_TOKEN = 'v04-acceptance-session-token'
NOW = int(datetime.now(timezone.utc).timestamp())
day = lambda n: NOW - n * 86400
iso = lambda n: datetime.fromtimestamp(NOW - n * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def main():
    settings = Settings()
    engine = create_engine(settings.database_url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for table in (m.Notification, m.Event, m.Digest, m.Delivery, m.LegacyRecord, m.Artifact,
                      m.Candidate, m.JobAccess, m.Job, m.Issue, m.Analysis, m.Subscription,
                      m.Repository, m.BrowserSession, m.Account, m.Quota, m.HttpCache):
            s.exec(delete(table))
        s.commit()

        user = m.Account(id='u1', github_id='1', login='czm233emmm', avatar_url='',
                         email='czm233emmm@gmail.com', email_verified=True, email_enabled=True,
                         email_mode='both', timezone='Asia/Shanghai', digest_hour=8)
        s.add(user)
        s.add(m.BrowserSession(digest=digest(SESSION_TOKEN), user_id='u1', csrf='seed-csrf', expires_at=NOW + 30 * 86400))
        s.commit()

        repos = {
            'pallets/itsdangerous': m.Repository(id='r1', github_id='101', full_name='pallets/itsdangerous',
                description='Various helpers to pass data to untrusted environments and back.', stars=2900,
                pushed_at=day(2), external_merge_at=day(11), signals_checked_at=NOW - 3600, signals_coverage_since=day(400), next_scan=NOW + 86400),
            'encode/httpx': m.Repository(id='r2', github_id='102', full_name='encode/httpx',
                description='A next generation HTTP client for Python.', stars=13200,
                pushed_at=NOW - 5 * 3600, external_merge_at=day(3), signals_checked_at=NOW - 3600, signals_coverage_since=day(400), next_scan=NOW + 86400),
            'shibing624/agentica': m.Repository(id='r3', github_id='103', full_name='shibing624/agentica',
                description='Agent framework for building AI agents.', stars=1800,
                pushed_at=day(1), external_merge_at=day(26), signals_checked_at=NOW - 3600, signals_coverage_since=day(300), next_scan=NOW + 86400),
            'grpc/grpc-node': m.Repository(id='r4', github_id='104', full_name='grpc/grpc-node',
                description='gRPC for Node.js.', stars=4100,
                pushed_at=day(3), external_merge_at=day(19), signals_checked_at=NOW - 3600, signals_coverage_since=day(300), next_scan=NOW + 86400),
        }
        s.add_all(repos.values())
        s.commit()
        for rid, repo in enumerate(repos.values(), 1):
            s.add(m.Subscription(id=f's{rid}', user_id='u1', repo_id=repo.id,
                                 watched_issues=[221] if repo.id == 'r4' else []))
        s.commit()

        def issue(repo_id, number, title, title_zh='', difficulty='未分析', status='open', state='open',
                  created_days=8, comments=0, **kw):
            return m.Issue(repo_id=repo_id, number=number, title=title, title_zh=title_zh, title_zh_source=title if title_zh else '',
                url=f'https://github.com/x/y/issues/{number}', created_at=iso(created_days), updated_at=iso(max(0, created_days - 2)),
                state=state, status=status, difficulty=difficulty, comments=comments, checked_at=NOW - 3600, **kw)

        analysis_942 = dict(
            summary='Webhook 签名目前只支持单一静态密钥，密钥泄漏后需要手动替换并同步所有消费方。作者希望引入密钥轮换机制，允许多个密钥并存并在宽限期内完成切换。',
            problem='`URLSafeTimedSerializer` 在实例化时绑定单一 secret key，无法平滑轮换。讨论中出现 `secret_rotator` 兼容层与双密钥宽限期两条路线；维护者倾向先落地双密钥宽限期，并保持对旧序列化格式的兼容。',
            plan='1. 为 `TimestampSigner` 新增多密钥构造参数（向后兼容）\n2. 反序列化端在宽限期内依次尝试历史密钥\n3. 文档补充分阶段轮换操作指南\n4. 兼容 Python ≥3.10 与旧版 serializer 的行为差异',
            evidence=[{'kind': 'pr', 'reason': '关联 PR 检查：未发现引用 #942 的 PR', 'repo': 'pallets/itsdangerous', 'number': 950, 'merged': False, 'open': False, 'author': 'someone'},
                      {'kind': 'assignee', 'reason': '指派检查：无 assignee'},
                      {'kind': 'claim', 'reason': '认领检查：3 条评论均为方案讨论，无认领声明'}])
        rows = [
            # 可上手
            issue('r1', 942, 'Support key rotation for Webhook signing secrets', '支持为 Webhook 签名密钥配置轮换', '中等', comments=3, **analysis_942),
            issue('r2', 1887, 'Docs: add migration guide for v0.28 → 1.x', '补充 0.28 到 1.x 的迁移指南', '简单'),
            issue('r4', 221, 'feat: expose per-request trace context', '为每次请求暴露链路追踪上下文', '中等', comments=2),
            issue('r1', 944, 'Add expires_every option to TimedSerializer', '为 TimedSerializer 增加 expires_every 选项', '简单'),
            issue('r3', 78, 'Support tool result streaming in agent loop', '在智能体循环中支持工具结果流式返回', '简单'),
            # 有人做了
            issue('r2', 452, 'Race condition in retry middleware under HTTP/2', 'HTTP/2 下重试中间件的竞态条件', '困难', status='taken-pr', comments=6),
            issue('r3', 77, 'Add streaming tool-call events to the agent loop', '为智能体循环添加流式工具调用事件', '中等', status='taken-assignee', comments=4),
            issue('r4', 310, 'Flaky test: test_proxy_pool_exhaustion', '不稳定测试：代理池耗尽用例', '简单', status='taken-claim', comments=2,
                  claim={'author': 'dev-chen', 'reason': '评论声明认领修复', 'until': NOW + 12 * 86400, 'url': 'https://github.com/x/y/issues/310#issuecomment-1'}),
            # 已关闭
            issue('r1', 56, 'Deprecate Python 3.9 support', '弃用 Python 3.9 支持', '简单', state='closed', status='closed', created_days=55),
            issue('r1', 943, 'Typo in signing docs page', '签名文档页的笔误', state='closed', status='closed', created_days=9),
            # 未入榜
            issue('r4', 3300, 'Migrate build system to Bazel', '构建系统迁移到 Bazel', '困难', created_days=12),
            issue('r2', 1812, 'Refactor transport layer error handling', '重构传输层错误处理', '简单', created_days=120),
            # 待分析
            issue('r2', 1201, 'Memory leak in connection pool under HTTP/2', '连接池在 HTTP/2 下的内存泄漏'),
        ]
        s.add_all(rows)
        s.commit()

        jobs = [
            m.Job(id='j1', kind='scan', status='completed', payload={'repo_id': 'r1'}, attempts=1, finished_at=day(0) - 7200, result={'count': 18}, created_at=day(0) - 10800),
            m.Job(id='j2', kind='analyze_issue', status='completed', payload={'repo_id': 'r1', 'issue_id': rows[0].id}, attempts=1, finished_at=day(0) - 7000, result={'count': 1}, created_at=day(0) - 9000),
            m.Job(id='j3', kind='analyze_repo', status='running', payload={'repo_id': 'r2'}, attempts=1, created_at=day(0) - 3000, available_at=day(0) - 3000),
            m.Job(id='j4', kind='digest', status='pending', payload={'repo_id': ''}, attempts=0, created_at=day(0) - 2000, available_at=day(0) + 3600),
            m.Job(id='j5', kind='mail', status='failed', payload={'repo_id': 'r3'}, attempts=3, error='SMTP 服务器连接超时', created_at=day(1), finished_at=day(1) + 600),
            m.Job(id='j6', kind='translate_titles', status='completed', payload={'repo_id': 'r4'}, attempts=1, finished_at=day(1), result={'cached': True}, created_at=day(1) - 600),
            m.Job(id='j7', kind='watch_issues', status='completed', payload={'repo_id': 'r4'}, attempts=1, finished_at=day(0) - 1000, created_at=day(0) - 2000),
            m.Job(id='j8', kind='candidates', status='pending', payload={'repo_id': ''}, attempts=0, created_at=day(0) - 500, available_at=day(0) + 7200),
        ]
        s.add_all(jobs)
        s.commit()
        s.add_all(m.JobAccess(job_id=j.id, user_id='u1') for j in jobs)
        s.commit()

        events = [
            m.Event(id=f'e{i}', key=f'ev-{i}', repo_id=repo, issue_number=number, kind=kind, title=title, body=body, url=f'https://github.com/x/y/issues/{number}', created_at=day(age))
            for i, (repo, number, kind, title, body, age) in enumerate([
                ('r1', 942, 'opportunity', '新的可参与 Issue：Support key rotation for Webhook signing secrets', '难度中等，公开讨论显示无人在做。', 1),
                ('r2', 452, 'issue-taken', 'Issue 被认领：Race condition in retry middleware', '出现关联 PR #460，作者声明修复本 Issue。', 2),
                ('r1', 56, 'issue-closed', '机会了结：Deprecate Python 3.9 support', 'Issue 已关闭。', 3),
                ('r2', 780, 'release', 'httpx v0.28.1 发布', '修复了代理与 HTTP/2 的若干问题。', 4),
                ('r3', 77, 'tripwire', '功能判断变化：agent loop 流式事件', '维护者将流式工具调用提上路线图。', 5),
            ])]
        s.add_all(events)
        s.commit()
        s.add_all(m.Notification(id=f'n{i}', user_id='u1', event_id=f'e{i}', read_at=0 if i < 3 else day(1), created_at=day(5 - i)) for i in range(5))
        s.commit()

        s.add_all([
            m.Digest(id='d1', user_id='u1', date=datetime.fromtimestamp(day(1), timezone.utc).strftime('%Y-%m-%d'),
                     content='# 每日晨报\n\n## 关注动态\n\n- **pallets/itsdangerous** 出现新的可参与 Issue：[Support key rotation for Webhook signing secrets](https://github.com/x/y/issues/942)，难度中等，公开讨论显示无人在做。\n- **encode/httpx** #452 出现关联 PR，机会已被占据。\n\n## 候选仓库\n\n- 新增 Star：`tiangolo/fastapi` 近期发布 0.116，修复了多个文档问题。',
                     created_at=day(1), period_start=day(2), period_end=day(1)),
            m.Digest(id='d2', user_id='u1', date=datetime.fromtimestamp(day(2), timezone.utc).strftime('%Y-%m-%d'),
                     content='# 每日晨报\n\n## 关注动态\n\n- **grpc/grpc-node** 发布 v1.10.0，重写了负载均衡策略。\n\n> 提示：在监控清单中可以调整每个项目的新机会提醒条件。',
                     created_at=day(2), period_start=day(3), period_end=day(2)),
        ])
        s.commit()

        arch_old = m.Artifact(id='a1', repo_id='r1', path='architecture.md', revision='77bd1c40e9aa',
            content='# 架构\n\nitsdangerous 由三个核心抽象组成：Signer（HMAC 签名）、Serializer（结构序列化）、TimestampSigner（带时效）。',
            created_at=day(18))
        arch_new = m.Artifact(id='a2', repo_id='r1', path='architecture.md', revision='a3f9c21e77b0',
            content='# 架构\n\nitsdangerous 由四个核心抽象组成：Signer（HMAC 签名）、Serializer（结构序列化）、TimestampSigner（带时效）、MultiKeySigner（密钥轮换）。\n\n## 模块\n\n- `serializer.py` — 序列化与反序列化入口\n- `signer.py` — HMAC 签名与密钥派生\n- `urlsafe.py` — URL 安全的编解码封装',
            created_at=day(1))
        s.add(arch_old)
        s.commit()
        s.add(arch_new)
        s.commit()
        s.add_all([
            m.Artifact(id='a3', repo_id='r1', path='contribution.md', revision='c4520b7a31e9',
                content='# 贡献指南\n\n- Fork 后从 main 拉分支\n- 提交前跑 `pytest`\n- 维护者会在 48 小时内回应', created_at=day(20)),
            m.Artifact(id='a4', repo_id='r2', path='architecture.md', revision='0e83a1cf58d2',
                content='# 架构\n\nhttpx 分为 Client / Transport / Dispatcher 三层，同步与异步共享传输实现。', created_at=day(6)),
        ])
        s.commit()

        s.add_all([
            m.Delivery(id='dl1', user_id='u1', key='mail-1', recipient='czm233emmm@gmail.com',
                       subject='Gitwire 警报：新的可参与 Issue', body='...', status='sent', created_at=day(1), sent_at=day(1) + 300),
            m.Delivery(id='dl2', user_id='u1', key='mail-2', recipient='czm233emmm@gmail.com',
                       subject='Gitwire 每日晨报', body='...', status='pending', created_at=day(0) - 3600),
            m.Delivery(id='dl3', user_id='u1', key='mail-3', recipient='czm233emmm@gmail.com',
                       subject='Gitwire 验证邮件', body='...', status='failed', error='SMTP 服务器连接超时', created_at=day(1) - 7200),
        ])
        s.add_all([
            m.LegacyRecord(id='lg1', user_id='u1', source_key='old-doc-1', kind='document', repo='pallets/itsdangerous',
                           title='旧版架构笔记', content='# 旧版架构笔记\n\n迁移前的私人文档。', created_at=day(90)),
            m.LegacyRecord(id='lg2', user_id='u1', source_key='old-daily-1', kind='daily',
                           title='2026-07 晨报存档', content='# 2026-07 晨报存档\n\n迁移前的私人晨报。', created_at=day(80)),
        ])
        s.add_all([
            m.Candidate(id='c1', user_id='u1', github_id='201', full_name='tiangolo/fastapi', description='FastAPI framework, high performance, easy to learn', source='starred', starred_at=iso(2)),
            m.Candidate(id='c2', user_id='u1', github_id='202', full_name='encode/starlette', description='The little ASGI framework that shines', source='starred', starred_at=iso(5)),
            m.Candidate(id='c3', user_id='u1', github_id='203', full_name='python/cpython', description='The Python programming language', source='starred', starred_at=iso(9)),
            m.Candidate(id='c4', user_id='u1', github_id='204', full_name='psf/requests', description='A simple, yet elegant, HTTP library', source='starred', starred_at=iso(15)),
            m.Candidate(id='c5', user_id='u1', github_id='205', full_name='old/project', description='已忽略的候选', source='starred', starred_at=iso(20), ignored=True),
        ])
        s.commit()
    engine.dispose()
    print(f'种子数据已写入 {settings.database_url}')
    print(f'登录方式：cookie gitwire_session={SESSION_TOKEN}')


if __name__ == '__main__':
    main()
