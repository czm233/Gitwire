import asyncio
import json
from sqlmodel import Session, select
from app.hosted.models import Issue, Job, Repository
from app.hosted.translations import schedule_translations, translate_titles
from test_hosted import hosted, login


def setup_issue(rt):
    rt.settings.llm_api_key = 'test-model-key'
    rt.settings.llm_model = 'test-model'
    with Session(rt.engine) as s:
        row = Issue(id='translation-issue', repo_id='repo', number=158,
                    title='post_json treats HTTP 200 with an error body as success',
                    summary='这是问题摘要，不是标题翻译', url='https://github.com/public/project/issues/158')
        s.add(row); s.commit(); s.refresh(row)
        return row


def model_response(monkeypatch, callback):
    class Model:
        def __init__(self, *args, **kwargs): pass
        async def chat(self, system, prompt, temperature=0):
            return callback(json.loads(prompt))
        async def aclose(self): pass
    monkeypatch.setattr('app.hosted.analysis.LLMClient', Model)


def test_translation_batches_cache_and_search_without_overwriting_summary(hosted, monkeypatch):
    client, rt = hosted
    issue = setup_issue(rt)
    calls = []
    def reply(entries):
        calls.append(entries)
        assert entries == [{'id': issue.id, 'title': issue.title}]
        return json.dumps({'translations': [{'id': issue.id, 'title_zh': 'post_json 将带错误响应体的 HTTP 200 视为成功'}]})
    model_response(monkeypatch, reply)
    login(client)
    for _ in range(3):
        assert client.get('/api/issues').status_code == 200
    with Session(rt.engine) as s:
        jobs = s.exec(select(Job).where(Job.kind == 'translate_titles')).all()
        assert len(jobs) == 1
        job = jobs[0]
    assert asyncio.run(translate_titles(rt, job)) == {'count': 1}
    assert asyncio.run(translate_titles(rt, job))['cached']
    assert len(calls) == 1
    response = client.get('/api/issues', params={'q': '错误响应体'}).json()
    assert response['total'] == 1
    assert response['items'][0]['title_zh'].startswith('post_json 将')
    assert response['items'][0]['summary'] == issue.summary
    assert 'title_zh_source' not in response['items'][0]
    with Session(rt.engine) as s:
        row = s.get(Issue, issue.id); row.title = 'Changed title'; s.add(row); s.commit()
    assert client.get('/api/issues').json()['items'][0]['title_zh'] == ''
    assert client.get('/api/issues', params={'q': '错误响应体'}).json()['total'] == 0


def test_translation_rejects_stale_and_private_results(hosted, monkeypatch):
    _, rt = hosted
    issue = setup_issue(rt)
    job = Job(kind='translate_titles', payload={'repo_id': 'repo', 'issue_ids': [issue.id]})
    def reply(entries):
        with Session(rt.engine) as s:
            row = s.get(Issue, issue.id); row.title = 'New title'; s.add(row); s.commit()
        return json.dumps({'translations': [{'id': issue.id, 'title_zh': '旧标题的翻译'}]})
    model_response(monkeypatch, reply)
    assert asyncio.run(translate_titles(rt, job)) == {'count': 0}
    with Session(rt.engine) as s:
        assert not s.get(Issue, issue.id).title_zh
        repo = s.get(Repository, 'repo'); repo.public = False; s.add(repo); s.commit()
    assert asyncio.run(translate_titles(rt, job))['skipped']


def test_translation_rejects_wrong_ids_and_honors_budget(hosted, monkeypatch):
    import pytest
    from fastapi import HTTPException
    _, rt = hosted
    issue = setup_issue(rt)
    job = Job(kind='translate_titles', payload={'repo_id': 'repo', 'issue_ids': [issue.id]})
    model_response(monkeypatch, lambda entries: json.dumps({'translations': [{'id': 'other', 'title_zh': '错误匹配'}]}))
    with pytest.raises(ValueError):
        asyncio.run(translate_titles(rt, job))
    with Session(rt.engine) as s:
        assert not s.get(Issue, issue.id).title_zh
    rt.settings.model_repo_daily_limit = 1
    with pytest.raises(HTTPException) as exc:
        asyncio.run(translate_titles(rt, job))
    assert exc.value.status_code == 429
