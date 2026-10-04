import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
import pytest
from app.local_archive import compare_archives, import_archive, push_archive


def export(tmp_path, content='analysis', account='100'):
    source = tmp_path / 'export.zip'
    files = {'repositories/public/project/latest/overview.md': content.encode()}
    manifest = {'format': 'gitwire-export-v1', 'account': account, 'repositories': ['public/project'],
                'files': {p: hashlib.sha256(v).hexdigest() for p, v in files.items()}}
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        for name, value in files.items():
            archive.writestr(name, value)
    return source


def test_local_import_is_versioned_idempotent_and_has_no_remote(tmp_path):
    directory = tmp_path / 'archive'
    one = import_archive(export(tmp_path), directory)
    assert one['changed']
    assert not import_archive(export(tmp_path), directory)['changed']
    two = import_archive(export(tmp_path, 'updated analysis'), directory)
    assert two['snapshot'] != one['snapshot']
    def git(*args):
        return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()
    assert git('rev-list', '--count', 'HEAD') == '2'
    assert git('remote') == ''
    assert git('status', '--porcelain') == ''
    diff = compare_archives(directory)
    assert '-analysis' in diff and '+updated analysis' in diff
    assert (directory/'snapshots'/one['snapshot']/'repositories/public/project/latest/overview.md').read_text() == 'analysis'
    with pytest.raises(ValueError, match='不同账户'):
        import_archive(export(tmp_path, account='200'), directory)
    with pytest.raises(ValueError, match='源仓库'):
        push_archive(directory, 'git@github.com:public/project.git')
    with pytest.raises(ValueError, match='不含凭证'):
        push_archive(directory, 'https://token@github.com/public/backup.git')


def test_local_import_rejects_traversal_and_preserves_edits(tmp_path):
    source = export(tmp_path)
    with zipfile.ZipFile(source, 'a') as archive:
        archive.writestr('../outside.txt', 'bad')
    with pytest.raises(ValueError, match='不安全'):
        import_archive(source, tmp_path/'archive')
    assert not (tmp_path/'outside.txt').exists()
    directory = tmp_path/'archive'
    result = import_archive(export(tmp_path), directory, version=False)
    file = directory/'snapshots'/result['snapshot']/'repositories/public/project/latest/overview.md'
    file.write_text('my manual changes')
    with pytest.raises(ValueError, match='已有快照被修改'):
        import_archive(export(tmp_path), directory, version=False)
    assert file.read_text() == 'my manual changes'
    assert not (directory/'.git').exists()


def test_import_into_unrelated_directory_is_rejected(tmp_path):
    directory = tmp_path/'other-project'
    directory.mkdir()
    (directory/'README.md').write_text('user work')
    with pytest.raises(ValueError, match='空目录'):
        import_archive(export(tmp_path), directory)
    assert (directory/'README.md').read_text() == 'user work'
