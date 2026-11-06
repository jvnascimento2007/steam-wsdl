"""A real local console with prompts lacking newlines, like SteamCMD."""
import json
import queue
import sys
from pathlib import Path


def executable(root: Path, body: str = '') -> Path:
    path = root / 'fake-steamcmd'
    trace = root / 'console.jsonl'
    path.write_text(f'''#!{sys.executable}
import json, os, shlex, sys, time
from pathlib import Path
args = sys.argv
root = Path(args[args.index('+force_install_dir') + 1])
trace = Path({str(trace)!r})
def record(**data):
    with trace.open('a') as f: f.write(json.dumps(dict(process=os.getpid(), **data)) + '\\n')
def prompt():
    sys.stdout.write('\\x1b[1m\\nSteam>\\x1b[0m')
    sys.stdout.flush()
record(event='start', root=str(root), login_count=args.count('+login'))
def download(app, pid):
    record(event='download', item=pid)
    content = root / 'steamapps/workshop/content' / app / pid
    staging = root / 'steamapps/workshop/downloads' / app / pid
{''.join('    ' + line + chr(10) for line in body.splitlines())}
    if pid == '999':
        print('ERROR! Download item 999 failed (No subscription)', flush=True)
    else:
        content.mkdir(parents=True, exist_ok=True)
        (content / 'data').write_bytes(b'content')
        print(f'Success. Downloaded item {{pid}}', flush=True)
if '+workshop_download_item' in args:
    i=args.index('+workshop_download_item')
    download(*args[i+1:i+3])
else:
    prompt()
    for line in sys.stdin:
        command=shlex.split(line)
        if not command: continue
        if command[0] == 'quit':
            record(event='quit')
            break
        assert command[0] == 'workshop_download_item', command
        download(*command[1:3])
        prompt()
''')
    path.chmod(0o755)
    return path


def records(root: Path):
    return [json.loads(line) for line in (root / 'console.jsonl').read_text().splitlines()]


def work_queue(jobs):
    result = queue.Queue()
    for j in jobs: result.put(j)
    return result
