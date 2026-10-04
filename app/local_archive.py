"""Local-only archive tools. Never load hosted OAuth credentials or call GitHub APIs."""
import hashlib
import difflib
import json
from pathlib import Path
import re
import subprocess
import zipfile

MAX_BYTES = 50 * 1024 * 1024


def git(directory, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
        '-C', str(directory), *args], capture_output=True, text=True)
    if result.returncode:
        # Git/SSH errors can contain local authentication details; don't echo them.
        raise ValueError('Git 操作未完成，请检查本机 Git/SSH 配置和远端权限；没有执行强制覆盖')
    return result.stdout.strip()


def read_export(source):
    from app.hosted.export import safe_path
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > 100000 or sum(i.file_size for i in infos) > MAX_BYTES:
            raise ValueError('归档过大')
        names = [i.filename for i in infos]
        if len(set(names)) != len(names) or any(not safe_path(n) for n in names):
            raise ValueError('归档包含不安全或重复路径')
        if any((i.external_attr >> 16) & 0o170000 == 0o120000 for i in infos):
            raise ValueError('归档不允许符号链接')
        payloads = {i.filename: archive.read(i) for i in infos}
    try:
        manifest = json.loads(payloads.pop('manifest.json'))
        if manifest['format'] != 'gitwire-export-v1' or set(manifest['files']) != set(payloads):
            raise ValueError('归档格式不正确')
        for name, content in payloads.items():
            if hashlib.sha256(content).hexdigest() != manifest['files'][name]:
                raise ValueError('归档校验失败')
        if not isinstance(manifest['account'], str) or not manifest['account'].isdigit():
            raise ValueError('归档账户标识无效')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('归档清单无效') from exc
    return manifest, payloads


def import_archive(source: Path, directory: Path, version: bool = True):
    manifest, files = read_export(source)
    directory = directory.expanduser().absolute()
    if directory.is_symlink():
        raise ValueError('归档目录不能是符号链接')
    marker = directory / '.gitwire-archive.json'
    existing = {}
    if directory.exists() and any(directory.iterdir()):
        if not marker.is_file() or marker.is_symlink():
            raise ValueError('请选择空目录或已有的 Gitwire 归档目录')
        existing = json.loads(marker.read_text())
        if existing.get('account') != manifest['account']:
            raise ValueError('不同账户的归档必须保存到不同目录')
    if (directory / '.git').exists():
        if (directory / '.git').is_symlink() or git(directory, 'status', '--porcelain'):
            raise ValueError('本地归档有未提交修改，请先保存你的改动')
        if not version:
            raise ValueError('此目录已启用 Git 版本管理；请继续使用版本模式或选择新目录')
    # Immutable snapshots never overwrite manually edited analysis or notes.
    snapshot = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    destination = directory / 'snapshots' / snapshot
    for parent in (directory / 'snapshots', destination):
        if parent.is_symlink():
            raise ValueError('归档路径不能是符号链接')
    if destination.exists():
        for path, content in files.items():
            saved = destination / path
            if any(parent.is_symlink() for parent in [saved, *saved.parents]) or not saved.is_file() or saved.read_bytes() != content:
                raise ValueError('已有快照被修改，请保留修改并选择新的归档目录')
        return {'snapshot': snapshot, 'changed': False}
    destination.mkdir(parents=True)
    for path, content in files.items():
        output = destination / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    marker.write_text(json.dumps({'format': 1, 'account': manifest['account'],
        'repositories': sorted(set(existing.get('repositories', [])) | set(manifest['repositories'])),
        'snapshots': [*existing.get('snapshots', []), snapshot]}, indent=2))
    (directory / 'LATEST').write_text('snapshots/' + snapshot + '\n')
    if version:
        if not (directory / '.git').exists():
            git(directory, 'init', '-b', 'main')
        git(directory, 'add', '--', 'snapshots/' + snapshot, 'LATEST', '.gitwire-archive.json')
        git(directory, '-c', 'user.name=Gitwire Archive', '-c', 'user.email=archive@gitwire.local',
            'commit', '-m', 'Import Gitwire intelligence snapshot ' + snapshot[:12])
    return {'snapshot': snapshot, 'changed': True}


def compare_archives(directory: Path, before: str = '', after: str = '') -> str:
    directory = directory.expanduser().resolve()
    config = json.loads((directory / '.gitwire-archive.json').read_text())
    snapshots = config.get('snapshots', [])
    if len(snapshots) < 2 and not before:
        return '目前只有一个快照，再导入一次变化后的情报即可对比。'
    before, after = before or snapshots[-2], after or snapshots[-1]
    if any(not re.fullmatch(r'[0-9a-f]{64}', value) or value not in snapshots for value in (before, after)):
        raise ValueError('快照标识无效')
    def contents(snapshot):
        base = directory / 'snapshots' / snapshot
        manifest = json.loads((base / 'manifest.json').read_text())
        from app.hosted.export import safe_path
        result = {}
        for path in manifest['files']:
            target = base / path
            if not safe_path(path) or any(p.is_symlink() for p in [target, *target.parents]):
                raise ValueError('归档路径不安全')
            # Historical revisions are already represented by their latest document.
            if '/history/' not in path:
                result[path] = target.read_text().splitlines(keepends=True)
        return result
    old, new = contents(before), contents(after)
    changes = []
    for path in sorted(old.keys() | new.keys()):
        changes.extend(difflib.unified_diff(old.get(path, []), new.get(path, []),
            fromfile=f'{before[:12]}/{path}', tofile=f'{after[:12]}/{path}'))
    return ''.join(changes) or '两个快照的正文没有变化。'


def push_archive(directory: Path, remote: str):
    directory = directory.expanduser().resolve()
    marker = directory / '.gitwire-archive.json'
    if not marker.is_file() or not (directory / '.git').exists():
        raise ValueError('请先导入到启用了 Git 版本管理的本地归档目录')
    # This command is explicitly invoked by the local user and uses their Git
    # credential helper/SSH agent. URLs cannot embed tokens or target a source repo.
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', remote)
    if not match:
        raise ValueError('请输入不含凭证的 GitHub HTTPS 或 SSH 仓库地址')
    repo = match.group(1).removesuffix('.git')
    config = json.loads(marker.read_text())
    if repo.casefold() in {name.casefold() for name in config['repositories']}:
        raise ValueError('备份仓库不能是监控的源仓库，请单独创建情报仓库')
    if git(directory, 'status', '--porcelain'):
        raise ValueError('归档还有未提交修改，请先检查并保存')
    git(directory, 'push', remote, 'HEAD:refs/heads/main')
