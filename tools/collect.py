#!/usr/bin/env python3
"""Turn the mihomo group-delay result into the verified-alive id list and
POST it to the subscription Worker. Token comes from env SUB_TOKEN and is
never printed."""
import json
import os
import sys
import urllib.request


def main():
    delays = json.load(open('delays.json'))
    data = json.load(open('candidates.json'))
    by_name = {c['p']['name']: c['id'] for c in data['candidates']}
    alive_ids, alive_ms = [], {}
    for name, ms in (delays or {}).items():
        if isinstance(ms, (int, float)) and ms > 0 and name in by_name:
            alive_ids.append(by_name[name])
            alive_ms[by_name[name]] = ms
    tested = len(data['candidates'])
    print(f"tested={tested} alive={len(alive_ids)}")
    token = os.environ.get('SUB_TOKEN', '')
    if not token:
        print('SUB_TOKEN missing; results kept local only')
        json.dump({'ids': alive_ids, 'delays': alive_ms}, open('verified.json', 'w'))
        return 0
    body = json.dumps({'ids': alive_ids, 'tested': tested}).encode()
    req = urllib.request.Request(
        f'https://sub.glmd.de5.net/{token}/verified',
        data=body, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60) as r:
        print('worker response:', r.status, r.read()[:120].decode('utf-8', 'replace'))


if __name__ == '__main__':
    sys.exit(main())
