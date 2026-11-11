#!/usr/bin/env python3
"""Turn the mihomo group-delay result into the verified-alive id list and
POST it to the subscription Worker. Token comes from env SUB_TOKEN and is
never printed."""
import json
import os
import sys
import urllib.error
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
        data=body, method='POST', headers={
            'Content-Type': 'application/json',
            # A default Python-urllib agent gets 403'd by Cloudflare bot
            # heuristics; present as a regular browser client instead.
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print('worker response:', r.status,
                  r.read()[:120].decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        print('worker rejected:', e.code,
              e.read()[:200].decode('utf-8', 'replace'))
        return 1


if __name__ == '__main__':
    sys.exit(main())
