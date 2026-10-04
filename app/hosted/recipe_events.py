"""Replay the isolated recipe ledger into public events and personal alerts.

The SQLite ledger remains replayable after a crash between local publication and
the PostgreSQL commit. Its errors and private legacy ledgers are never imported.
"""
from datetime import timezone
from sqlmodel import Session, select
from app.models import Alert
from app.hosted.corpus import fingerprint
from app.hosted.models import Account, Event, Notification, Repository, Subscription, now

RECIPE_KINDS = {'tripwire': 'feature-tripwire', 'breaking': 'bug-watch',
                'cve': 'cve-scan', 'release': None}


def recipe_enabled(sub, kind):
    if kind not in RECIPE_KINDS:
        return False
    recipe = RECIPE_KINDS[kind]
    # Release analysis accompanies the repository recipes, not the Issue-only lane.
    return recipe in sub.recipes if recipe else bool(set(sub.recipes) - {'issue-radar'})


def import_recipe_events(source_engine, engine, repo_id):
    imported = 0
    with Session(source_engine) as source:
        with Session(engine) as s:
            repo = s.get(Repository, repo_id)
            if not repo or not repo.public:
                return 0
            name = repo.full_name
        rows = source.exec(select(Alert).where(Alert.repo == name,
            Alert.kind.in_(RECIPE_KINDS)).order_by(Alert.id).execution_options(yield_per=100))
        for alert in rows:
            stamp = alert.ts.replace(tzinfo=timezone.utc) if alert.ts.tzinfo is None else alert.ts
            created = int(stamp.timestamp())
            key = fingerprint(['recipe-ledger-v1', repo_id, alert.id, stamp.isoformat(),
                               alert.kind, alert.title, alert.body])
            with Session(engine) as s:
                # Only a new event fans out. A later change in preference must not
                # replay every old analysis as if it had just happened.
                if s.exec(select(Event.id).where(Event.key == key)).first():
                    continue
                repo = s.get(Repository, repo_id)
                if not repo or not repo.public:
                    break
                event = Event(key=key, repo_id=repo_id, kind=alert.kind,
                    title=alert.title, body=alert.body, created_at=created,
                    url=f'https://github.com/{repo.full_name}' + ('/releases' if alert.kind == 'release' else ''))
                s.add(event); s.flush()
                subs = s.exec(select(Subscription).where(Subscription.repo_id == repo_id,
                    Subscription.paused == False, Subscription.created_at <= created)).all()
                for sub in subs:
                    user = s.get(Account, sub.user_id)
                    if not user or alert.kind not in user.notify_kinds or not recipe_enabled(sub, alert.kind):
                        continue
                    if sub.keywords and not any(word.casefold() in (alert.title + ' ' + alert.body).casefold()
                                               for word in sub.keywords.split()):
                        continue
                    s.add(Notification(user_id=sub.user_id, event_id=event.id, created_at=created))
                s.commit()
                imported += 1
    return imported
