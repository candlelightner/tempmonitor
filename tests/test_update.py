import os
from pathlib import Path
import shutil
import subprocess


def test_update_fast_forwards_and_preserves_conflicting_local_edits(tmp_path):
    source = Path(__file__).resolve().parents[1]
    remote = tmp_path / 'remote'
    checkout = tmp_path / 'checkout'

    def git(*args, cwd=remote):
        return subprocess.check_output(['git', *args], cwd=cwd, text=True).strip()

    remote.mkdir()
    git('init', '-b', 'main')
    git('config', 'user.email', 'test@example.test')
    git('config', 'user.name', 'Test')
    (remote / 'file').write_text('first\n')
    git('add', 'file')
    git('commit', '-m', 'first')
    git('clone', str(remote), str(checkout), cwd=tmp_path)
    (checkout / 'scripts').mkdir()
    shutil.copy(source / 'scripts/update.sh', checkout / 'scripts/update.sh')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    sudo = binaries / 'sudo'
    sudo.write_text('#!/bin/sh\nprintf "deploy\\n" >> "$DEPLOY_LOG"\nexit "${DEPLOY_RESULT:-0}"\n')
    sudo.chmod(0o755)
    log = tmp_path / 'deploy.log'
    env = {**os.environ, 'PATH': f'{binaries}:{os.environ["PATH"]}', 'DEPLOY_LOG': str(log)}

    def update(**extra):
        return subprocess.run(['bash', str(checkout / 'scripts/update.sh')], env={**env, **extra},
                              capture_output=True, text=True)

    assert update(DEPLOY_RESULT='1').returncode != 0
    assert not (checkout / '.git/tempmonitor-deployed').exists()
    assert update().returncode == 0
    assert update().returncode == 0
    assert log.read_text().count('deploy') == 2
    (remote / 'file').write_text('second\n')
    git('commit', '-am', 'second')
    assert update().returncode == 0
    assert git('rev-parse', 'HEAD', cwd=checkout) == git('rev-parse', 'HEAD')
    (checkout / 'file').write_text('local edits\n')
    (remote / 'file').write_text('third\n')
    git('commit', '-am', 'third')
    assert update().returncode != 0
    assert (checkout / 'file').read_text() == 'local edits\n'
    assert log.read_text().count('deploy') == 3
