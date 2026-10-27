"""Export runnable desktop sources without publishing runtime data or secrets."""

import argparse
import ast
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize


PACKAGE = Path(__file__).resolve().parents[1]
SHARED = '\u5171\u4eab\u5185\u5b58\u7248'
SCANNERS = ('\u76d8\u4e2d.py', 'ths\u7b5b\u9009.py')
SECRETS = ('REDIS_PASSWORD', 'DINGTALK_WEBHOOK', 'DINGTALK_CONDITION3_WEBHOOK')
PROGRAMS = {
    'qmt_shm_bridge.py', 'external_runner.py', 'strategy_runtime.py',
    'performance_monitor.py', 'launcher_gui.py', 'launcher_core.py',
    'launcher_schedule.py', 'launch_date.py', 'date_picker.py', 'log_policy.py',
    'stream_events.py', 'install_daily_start.ps1', 'README.md', '.gitignore',
    '\u6253\u5f00\u7b56\u7565\u63a7\u5236\u53f0.vbs',
    '\u68c0\u67e5\u5171\u4eab\u5185\u5b58.cmd',
    '\u542f\u52a8\u76d8\u4e2d\u5916\u90e8\u7a0b\u5e8f.cmd',
    '\u542f\u52a8\u76d8\u4e2d\u4e0eTHS\u5916\u90e8\u7a0b\u5e8f.cmd',
    '\u542f\u52a8THS\u5916\u90e8\u7a0b\u5e8f.cmd',
}


def write_changed(path, data):
    if not path.exists() or path.read_bytes() != data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def sanitize(data, label):
    encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
    text = data.decode(encoding)
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    replacements = []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in SECRETS:
                found.add(target.id)
                replacements.append((node.lineno - 1, node.end_lineno,
                                     '%s = os.environ.get("%s", "")\n' % (target.id, target.id)))
    required = {'REDIS_PASSWORD'}
    if label == SCANNERS[0]:
        required.update(SECRETS)
    if not required <= found:
        raise RuntimeError('Expected credential settings not found in ' + label)
    for start, end, replacement in reversed(replacements):
        lines[start:end] = [replacement]
    result = ''.join(lines).encode(encoding)
    # Only credential assignments may differ. All business AST nodes stay identical.
    def business(payload):
        parsed = ast.parse(payload.decode(encoding))
        parsed.body = [n for n in parsed.body if not (
            isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id in SECRETS)]
        return ast.dump(parsed, include_attributes=False)
    if business(data) != business(result):
        raise RuntimeError('Business code changed during export: ' + label)
    return result


def adapt_tests(target, hashes):
    for name in ('test_end_to_end.py', 'test_strategy_parity.py'):
        path = target / 'tests' / name
        source = path.read_text(encoding='utf-8')
        replacements = []
        # Replace only approved 64-character source digests, not business assertions.
        original_hashes = {'9637b658a3aad4ba8a3c2be3266857751529a156118d4e9a22d9f96f10dba66e': hashes[SCANNERS[0]],
                           '33edda78dfe5092d29383005b2f99bdcb7e5df9e6eaaf1d719d26ac1fe706980': hashes[SCANNERS[1]]}
        for old, new in original_hashes.items():
            if old in source:
                source = source.replace(old, new)
                replacements.append(old)
        if not replacements:
            raise RuntimeError('Review changed source revision tests: ' + name)
        ast.parse(source)
        write_changed(path, source.encode('utf-8'))
    path = target / 'tests' / 'test_limit_gap_and_tick_date.py'
    source = path.read_text(encoding='utf-8')
    old = "source = subprocess.run(['git', 'show', 'HEAD:\u76d8\u4e2d.py'], cwd=str(ROOT.parent),\n                                check=True, capture_output=True).stdout"
    new = "source = (ROOT / 'tests' / 'fixtures' / 'panzhong_before_limit_gap.py').read_bytes()"
    if old not in source:
        raise RuntimeError('Review changed legacy parity test before export')
    write_changed(path, source.replace(old, new).encode('utf-8'))


def validate_export(inputs):
    private_values = set()
    for path, data in inputs.items():
        if path.name not in SCANNERS:
            continue
        encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
        for node in ast.parse(data.decode(encoding)).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and node.targets[0].id in SECRETS):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, str) and value:
                    private_values.add(value.encode('utf-8'))
                    private_values.update(x.encode('utf-8') for x in re.findall(r'access_token=([^&\s]+)', value))
    for path in PACKAGE.rglob('*'):
        if (not path.is_file() or set(path.relative_to(PACKAGE).parts).intersection(
                {'__pycache__', 'logs', 'runtime', 'backups', '.pytest_cache'})):
            continue
        data = path.read_bytes()
        if any(value in data for value in private_values):
            raise RuntimeError('Private credential found; do not upload: ' + str(path.relative_to(PACKAGE)))


def export(source):
    if source == PACKAGE or PACKAGE in source.parents or source in PACKAGE.parents:
        raise RuntimeError('Source and export directories must be separate')
    source_shared = source / SHARED
    if not source_shared.is_dir():
        raise RuntimeError('Shared-memory source directory missing')
    target = PACKAGE / SHARED
    files = [source_shared / name for name in sorted(PROGRAMS)]
    files.extend(sorted((source_shared / 'docs').glob('*.md')))
    files.extend(sorted((source_shared / 'tests').glob('*.py')))
    files.extend(sorted((source_shared / 'tests' / 'fixtures').glob('*.json')))
    # Capture a consistent export. The desktop strategy can keep running.
    inputs = {path: path.read_bytes() for path in files + [source / x for x in SCANNERS]}
    hashes = {}
    for name in SCANNERS:
        clean = sanitize(inputs[source / name], name)
        write_changed(PACKAGE / name, clean)
        hashes[name] = hashlib.sha256(clean).hexdigest()
    for path in files:
        data = inputs[path]
        if path.name == '.gitignore':
            data += b'\nlogs/\nruntime/\n*.log\n.env\n.env.*\n*.local.*\n'
        if path.name == 'launcher_core.py':
            data = data.rstrip(b'\r\n') + b'\n'
        write_changed(target / path.relative_to(source_shared), data)
    fixture = target / 'tests' / 'fixtures' / 'panzhong_before_limit_gap.py'
    if not fixture.exists():
        # Immutable pre-marker baseline, copied once and credential-sanitized.
        baseline = subprocess.run(['git', 'show', 'd3cde27:' + SCANNERS[0]], cwd=source,
                                  check=True, capture_output=True).stdout
        write_changed(fixture, sanitize(baseline, SCANNERS[0]))
    adapt_tests(target, hashes)
    manifest = {'description': 'Public export hashes; credential assignments excluded from business parity check.',
                'sha256': hashes}
    write_changed(PACKAGE / 'source_manifest.json',
                  (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    if any(path.read_bytes() != original for path, original in inputs.items()):
        raise RuntimeError('Desktop source changed during export; export again before publishing')
    validate_export(inputs)
    print('Exported %d files. Business code AST unchanged; credentials removed.' % (len(files) + 2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path.home() / 'Desktop' / 'qmt')
    parser.add_argument('--test', action='store_true', help='Run offline shared-memory regression tests')
    args = parser.parse_args()
    export(args.source.resolve())
    if args.test:
        subprocess.run([sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover',
                        '-s', str(PACKAGE / SHARED / 'tests'), '-v'], cwd=PACKAGE, check=True)
    print('Review git diff; commit only qmt_soft. Desktop originals were not modified.')


if __name__ == '__main__':
    main()
