"""e2e_dashboard 状态机回归:停止(SIGINT)打断的用例应记为「取消」,不得显示成「失败」。"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import e2e_dashboard as dash  # noqa: E402


def finish_case(status: str, error: str = '') -> dict:
    """构造一条运行中的用例,喂给它指定 status/error 的 test-finished 事件,返回用例终态。"""
    case = {'title': '样例用例', 'file': 'x.e2e.ts', 'kind': 'test', 'ai': False, 'smoke': False,
            'state': 'running', 'durationMs': None, 'note': '', 'info': ''}
    dash.state['cases'] = [case]
    event = {'type': 'test-finished', 'title': '样例用例', 'status': status}
    if error:
        event['error'] = error
    dash.apply_event(event)
    return case


def test_interrupted_maps_to_cancelled():
    # 2026-10-12 用户按「停止」后被 SIGINT 打断的用例显示红色「× 失败」;修复后应为灰色「— 取消」
    case = finish_case('interrupted')
    assert case['state'] == 'cancelled'
    # 中断备注是一句人话,不得把 Playwright 内部错误对象堆栈铺在界面上
    assert case['note'] == '被停止指令打断,本条未跑完(不是测试失败)'
    assert 'INTERRUPTED' not in case['note']


def test_failed_case_keeps_error_note():
    # 真失败仍要展示错误信息,供定位问题(别把修复误伤到失败场景)
    case = finish_case('failed', error='expect(文本) 收到的却是另一段文本')
    assert case['state'] == 'failed'
    assert 'expect' in case['note']


def test_demo_cursor_only_for_human_watching_runs():
    # 2026-10-12 用户确立:勾选「显示浏览器窗口/录制回放视频」的运行必须带演示光标;静默回归不注入
    # 2026-10-13 拟人化节奏:演示运行同时启用输入节流代理(E2E_DEMO_PACE),
    # 点击真实落在光标滑到之后;带窗口的运行再加 E2E_DEMO_HEADED 让演示浏览器开窗口
    assert dash.demo_cursor_env(headed=True, video=False) == {
        'E2E_DEMO_CURSOR': '1', 'E2E_DEMO_PACE': '1', 'E2E_DEMO_HEADED': '1'}
    assert dash.demo_cursor_env(headed=False, video=True) == {
        'E2E_DEMO_CURSOR': '1', 'E2E_DEMO_PACE': '1'}
    assert dash.demo_cursor_env(headed=True, video=True) == {
        'E2E_DEMO_CURSOR': '1', 'E2E_DEMO_PACE': '1', 'E2E_DEMO_HEADED': '1'}
    assert dash.demo_cursor_env(headed=False, video=False) == {}


def test_trials_marked_queued_then_swept():
    # 2026-10-13 用户反馈"勾选 3 张使命卡,日志只显示启动使命 1,像漏跑了":
    # 顺序执行期间未轮到的卡应显示「排队中」,轮次结束/被停止后不得残留该状态
    dash.state['trials'].clear()
    dash.mark_trials_queued([1, 2, 3])
    assert all(dash.state['trials'][c]['status'] == 'queued' for c in (1, 2, 3))
    dash.state['trials'][2]['status'] = 'running'  # 模拟第 2 张已开跑
    dash.sweep_queued_trials([1, 2, 3])
    assert dash.state['trials'][1]['status'] == 'idle'   # 没轮到的回到「未运行」
    assert dash.state['trials'][2]['status'] == 'running'  # 在跑/跑完的不受影响


def test_single_trial_sets_running_then_finishes(monkeypatch):
    # 2026-10-15 用户反馈:使命卡单独「▶ 运行」后停不下来——单卡运行也要把全局状态
    # 置 running(停止按钮才会亮起),跑完/被停止后落 finished/stopped 并复位停止标志
    launched = []
    monkeypatch.setattr(dash, 'launch_charter', lambda c, headed=False: launched.append(c))
    dash.state['status'] = 'idle'
    dash.stop_requested[0] = False
    dash.run_single_trial(2)
    assert launched == [2]
    assert dash.state['status'] == 'finished'
    dash.stop_requested[0] = True  # 模拟运行中按了停止
    dash.run_single_trial(2)
    assert dash.state['status'] == 'stopped'
    assert dash.stop_requested[0] is False  # 标志复位,不拖累下一轮


def test_status_mapping_semantics_unchanged():
    # 守住既有语义:通过/失败/超时/跳过/主动取消,防止后续改动悄悄漂移
    assert finish_case('passed')['state'] == 'passed'
    assert finish_case('failed')['state'] == 'failed'
    assert finish_case('timed-out')['state'] == 'failed'
    assert finish_case('skipped')['state'] == 'skipped'
    assert finish_case('cancelled')['state'] == 'cancelled'


def test_restored_trials_marked_historical(tmp_path, monkeypatch):
    # 2026-10-14 用户反馈:驾驶舱刚启动,67 号使命卡就显示「已完成」,像本轮偷偷跑过;
    # 从磁盘报告恢复的是历史结果,必须带 restored+ran_at,前端才有依据显示「上次通过 · 时间」
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'ai-trial-report.md').write_text(
        '# AI 试用报告\n\n## 使命 1:冒烟走查(访客 · 266s · 退出码 0)\n\n## 使命 2:导入走查(登录 · 12s · 退出码 2)\n')
    monkeypatch.setattr(dash, 'ROOT', tmp_path)  # 报告根指到临时目录,不读真实 data/
    monkeypatch.setattr(dash, 'TRIAL_VIDEOS', tmp_path / 'data' / 'ai-trial-videos.json')
    trials = dash.initial_trials()
    assert set(trials) == {1, 2}
    for entry in trials.values():
        assert entry['restored'] is True  # 缺了历史标记,前端就会冒充本轮「已完成」
        assert re.fullmatch(r'\d{2}-\d{2} \d{2}:\d{2}', entry['ran_at'])  # mm-dd hh:mm,直接拼进「上次通过 · …」标签
    assert trials[1]['status'] == 'done' and trials[1]['exit'] == 0 and trials[1]['duration_s'] == 266
    assert trials[2]['exit'] == 2  # 上次有发现的卡同样恢复,退出码原样保留


def test_finished_trial_not_marked_restored():
    # 本轮真实跑完的使命卡不得带 restored 标记,否则刚跑完也显示「上次通过」
    entry = dash.finished_trial('done', 0, 120, '')
    assert entry == {'status': 'done', 'exit': 0, 'duration_s': 120, 'video': ''}
    assert 'restored' not in entry and 'ran_at' not in entry


def test_launch_charter_env_respects_headed(monkeypatch, tmp_path):
    # 2026-10-14 用户反馈:没勾「显示浏览器窗口」,运行使命卡仍直接弹出浏览器窗口;
    # launch_charter 必须把 headed 传给 demo_cursor_env:未勾选 → 无 E2E_DEMO_HEADED(无头录像),勾选 → 窗口可见
    captured = {}

    class FakeProc:  # 替身进程:launch_charter 只消费 stdout/wait/returncode/pid
        returncode = 0
        pid = 41234
        stdout = []

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return FakeProc()

    monkeypatch.setattr(dash.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(dash, 'FRONTEND', tmp_path)  # .e2e/results 不存在 → 跳过录像复制,不碰真实数据
    dash.launch_charter(1, headed=False)
    assert captured['env']['E2E_DEMO_CURSOR'] == '1'  # 走查录像给人看:演示光标照常注入
    assert 'E2E_DEMO_HEADED' not in captured['env']  # 没勾「显示浏览器窗口」→ 无头运行,不弹窗
    dash.launch_charter(1, headed=True)
    assert captured['env']['E2E_DEMO_HEADED'] == '1'  # 勾选 → 演示浏览器开窗口
