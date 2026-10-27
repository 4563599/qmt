"""Push one reviewed qmt_soft commit using GitHub REST and Git Credential Manager.

The REST route is useful when Git's HTTPS transport is unavailable. Credentials
stay in memory; only already committed qmt_soft files can be uploaded.
"""

import argparse
import ast
import base64
import io
import os
from pathlib import Path
import subprocess
import sys
import tokenize

import requests


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = '4563599/qmt'
API = 'https://api.github.com/repos/' + REPOSITORY


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True).stdout


def credential():
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    result = subprocess.run(['git', 'credential', 'fill'], cwd=ROOT, env=env,
                            input=b'protocol=https\nhost=github.com\n\n',
                            capture_output=True, timeout=20)
    fields = dict(line.split('=', 1) for line in result.stdout.decode('utf-8').splitlines() if '=' in line)
    token = fields.get('password')
    if result.returncode or not token:
        raise RuntimeError('GitHub login missing in Git Credential Manager; sign in first')
    return token


def request(session, method, path, payload=None):
    response = session.request(method, API + path, json=payload, timeout=45)
    if not response.ok:
        # API error bodies can echo source or credential-related inputs.
        raise RuntimeError('GitHub API %s %s: HTTP %d' % (method, path, response.status_code))
    return response.json()


def changes(base, head):
    names = git('diff', '--name-only', '-z', base, head).decode('utf-8').split('\0')
    names = [name for name in names if name]
    if not names or any(not name.startswith('qmt_soft/') for name in names):
        raise RuntimeError('Only a nonempty qmt_soft-only commit can use this uploader')
    forbidden = {'logs', 'runtime', 'backups', '__pycache__', '.pytest_cache', '.env'}
    for name in names:
        if (forbidden.intersection(Path(name).parts) or name.endswith(('.pyc', '.log'))
                or '.local.' in name or Path(name).name.startswith('.env.')):
            raise RuntimeError('Runtime/private file blocked: ' + name)
    entries = {}
    for record in git('ls-tree', '-r', '-z', head).split(b'\0'):
        if record:
            metadata, name = record.split(b'\t', 1)
            mode, kind, sha = metadata.decode('ascii').split()
            entries[name.decode('utf-8')] = (mode, kind, sha)
    return names, entries


def validate_blob(name, data):
    if name.endswith('.py'):
        encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
        for node in ast.parse(data.decode(encoding)).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if any(isinstance(t, ast.Name) and t.id in (
                        'REDIS_PASSWORD', 'DINGTALK_WEBHOOK', 'DINGTALK_CONDITION3_WEBHOOK') for t in node.targets):
                    if node.value.value:
                        raise RuntimeError('Literal credential blocked: ' + name)


def publish(push=False):
    origin = git('remote', 'get-url', 'origin').decode().strip().removesuffix('.git')
    if origin != 'https://github.com/' + REPOSITORY:
        raise RuntimeError('Unexpected Git origin; refusing upload')
    if git('branch', '--show-current').decode().strip() != 'master':
        raise RuntimeError('This uploader only publishes the master branch')
    if git('status', '--porcelain').strip():
        raise RuntimeError('Review and commit all intended changes first; working tree must be clean')
    head = git('rev-parse', 'HEAD').decode().strip()
    parents = git('rev-list', '--parents', '-n', '1', head).decode().split()
    if len(parents) != 2:
        raise RuntimeError('Merge/root commits require normal git push')
    base = parents[1]
    names, entries = changes(base, head)
    # Validate before creating any remote objects.
    for name in names:
        if name in entries:
            validate_blob(name, git('cat-file', 'blob', entries[name][2]))
    session = requests.Session()
    session.headers.update({'Authorization': 'Bearer ' + credential(),
                            'Accept': 'application/vnd.github+json',
                            'X-GitHub-Api-Version': '2022-11-28'})
    remote = request(session, 'GET', '/git/ref/heads/master')['object']['sha']
    if remote == head:
        git('update-ref', 'refs/remotes/origin/master', head)
        print('Already published: ' + head)
        return
    if remote != base:
        raise RuntimeError('Remote master changed or more than one commit is pending; fetch/review before publishing')
    print('Verified master parent; %d qmt_soft files changed.' % len(names))
    if not push:
        print('Dry run only. Add --push to upload this commit.')
        return
    elements = []
    uploaded = set()
    for index, name in enumerate(names, 1):
        if name not in entries:
            elements.append({'path': name, 'mode': '100644', 'type': 'blob', 'sha': None})
            continue
        mode, kind, sha = entries[name]
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise RuntimeError('Unsupported file mode/type: ' + name)
        if sha not in uploaded:
            data = git('cat-file', 'blob', sha)
            result = request(session, 'POST', '/git/blobs',
                             {'content': base64.b64encode(data).decode('ascii'), 'encoding': 'base64'})
            if result['sha'] != sha:
                raise RuntimeError('Uploaded blob hash mismatch')
            uploaded.add(sha)
        elements.append({'path': name, 'mode': mode, 'type': kind, 'sha': sha})
        if index % 10 == 0 or index == len(names):
            print('Uploaded %d/%d reviewed files.' % (index, len(names)), flush=True)
    base_tree = git('rev-parse', base + '^{tree}').decode().strip()
    tree = request(session, 'POST', '/git/trees', {'base_tree': base_tree, 'tree': elements})
    if tree['sha'] != git('rev-parse', head + '^{tree}').decode().strip():
        raise RuntimeError('Uploaded tree differs from reviewed local commit')
    fields = git('show', '-s', '--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI', head).decode().strip().split('\0')
    raw_commit = git('cat-file', 'commit', head)
    message = raw_commit.split(b'\n\n', 1)[1].decode('utf-8')
    result = request(session, 'POST', '/git/commits', {
        'message': message, 'tree': tree['sha'], 'parents': [base],
        'author': dict(zip(('name', 'email', 'date'), fields[:3])),
        'committer': dict(zip(('name', 'email', 'date'), fields[3:])),
    })
    if result['sha'] != head:
        raise RuntimeError('Commit metadata mismatch; branch was not changed')
    # Non-forced update is rejected if a concurrent remote commit is not our ancestor.
    request(session, 'PATCH', '/git/refs/heads/master', {'sha': head, 'force': False})
    verified = request(session, 'GET', '/git/ref/heads/master')['object']['sha']
    if verified != head:
        raise RuntimeError('Remote head changed during verification; inspect before retrying')
    git('update-ref', 'refs/remotes/origin/master', head)
    print('Published and verified: https://github.com/' + REPOSITORY + '/commit/' + head)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--push', action='store_true')
    args = parser.parse_args()
    try:
        publish(args.push)
    except (RuntimeError, subprocess.SubprocessError, requests.RequestException) as error:
        print('Publish failed: ' + (str(error) if isinstance(error, RuntimeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
