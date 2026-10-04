"""Personal opportunity rules evaluated over shared public evidence."""
from datetime import datetime, timezone
from sqlalchemy import and_, case, or_
from app.hosted.models import Issue, Repository, now

DEFAULTS = {'max_age_days': 30, 'max_comments': 15,
            'repo_pushed_within_days': 30, 'external_merge_within_days': 90}


def observation(issue, repo):
    """Public evidence only: never store subscriber identities or personal rules here."""
    return {'at': now(), 'issue': {key: getattr(issue, key) for key in
        ('title', 'body', 'difficulty', 'created_at', 'updated_at', 'comments', 'state')},
        'repo': {key: getattr(repo, key) for key in
        ('pushed_at', 'signals_checked_at', 'external_merge_at', 'signals_coverage_since')}}


def keyword_match(issue, sub):
    return not sub.keywords or any(word.casefold() in (issue.title + ' ' + issue.body).casefold()
                                   for word in sub.keywords.split())


def prior_matches(evidence, sub):
    from types import SimpleNamespace
    if not evidence or evidence.get('at', 0) < sub.created_at:
        return False
    issue = SimpleNamespace(**evidence['issue'])
    repo = SimpleNamespace(**evidence['repo'])
    return issue.state == 'open' and keyword_match(issue, sub) and not exclusion(issue, repo, sub, at=evidence['at'])


def change_matches(issue, repo, sub, before=None):
    if issue.number in sub.watched_issues:
        return keyword_match(issue, sub)
    return 'issue-radar' in sub.recipes and (
        (keyword_match(issue, sub) and not exclusion(issue, repo, sub)) or prior_matches(before, sub))


def epoch(value):
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
    except (ValueError, TypeError, AttributeError):
        return 0


def exclusion(issue, repo, sub=None, at=None):
    """Return a user-facing reason; missing evidence is never proof of opportunity."""
    at = at or now()
    rules = {**DEFAULTS, **(sub.opportunity if sub else {})}
    difficulties = sub.difficulty if sub else ['简单', '中等']
    if issue.difficulty not in difficulties:
        return '难度不在你的范围内'
    days = rules['repo_pushed_within_days']
    if days:
        if not repo.pushed_at or repo.signals_checked_at < at - 86400:
            return '仓库活跃证据待更新'
        if repo.pushed_at < at - days * 86400:
            return f'仓库超过 {days} 天无提交'
    days = rules['external_merge_within_days']
    if days:
        cutoff = at - days * 86400
        if repo.signals_checked_at < at - 86400 or (repo.external_merge_at < cutoff and
                (not repo.signals_coverage_since or repo.signals_coverage_since > cutoff)):
            return '外部贡献合并证据待更新'
        if repo.external_merge_at < cutoff:
            return f'近 {days} 天未发现外部 PR 合并'
    days = rules['max_age_days']
    created, updated = epoch(issue.created_at), epoch(issue.updated_at or issue.created_at)
    if days and created and updated and created < at - days * 86400 and updated < at - 14 * 86400:
        return f'创建超过 {days} 天且近 14 天无动静'
    count = rules['max_comments']
    if count and issue.comments > count:
        return f'评论超过 {count} 条，讨论过热'
    return ''


def exclusion_sql(sub=None, at=None):
    """Same rules in SQL so filtering, totals and pagination remain consistent."""
    at = at or now()
    rules = {**DEFAULTS, **(sub.opportunity if sub else {})}
    conditions = [(~Issue.difficulty.in_(sub.difficulty if sub else ['简单', '中等']), '难度不在你的范围内')]
    days = rules['repo_pushed_within_days']
    if days:
        conditions += [(or_(Repository.pushed_at == 0, Repository.signals_checked_at < at - 86400), '仓库活跃证据待更新'),
                       (Repository.pushed_at < at - days * 86400, f'仓库超过 {days} 天无提交')]
    days = rules['external_merge_within_days']
    if days:
        cutoff = at - days * 86400
        conditions += [(or_(Repository.signals_checked_at < at - 86400,
            and_(Repository.external_merge_at < cutoff, or_(Repository.signals_coverage_since == 0,
                Repository.signals_coverage_since > cutoff))), '外部贡献合并证据待更新'),
            (Repository.external_merge_at < cutoff, f'近 {days} 天未发现外部 PR 合并')]
    days = rules['max_age_days']
    if days:
        iso = lambda days: datetime.fromtimestamp(at - days * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        updated = case((Issue.updated_at == '', Issue.created_at), else_=Issue.updated_at)
        conditions.append((and_(Issue.created_at != '', Issue.created_at < iso(days), updated < iso(14)),
                           f'创建超过 {days} 天且近 14 天无动静'))
    count = rules['max_comments']
    if count:
        conditions.append((Issue.comments > count, f'评论超过 {count} 条，讨论过热'))
    return case(*conditions, else_='')
