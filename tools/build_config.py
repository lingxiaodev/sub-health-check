#!/usr/bin/env python3
"""candidates.json -> mihomo config with one CHECK group holding every node."""
import json
import sys

import yaml


def main():
    data = json.load(open('candidates.json'))
    proxies = [c['p'] for c in data['candidates']]
    cfg = {
        'mixed-port': 7891,
        'allow-lan': False,
        'mode': 'rule',
        'log-level': 'warning',
        'external-controller': '127.0.0.1:9090',
        'proxies': proxies,
        'proxy-groups': [
            {'name': 'CHECK', 'type': 'select',
             'proxies': [p['name'] for p in proxies]},
        ],
        'rules': ['MATCH,CHECK'],
    }
    with open('config.yaml', 'w') as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    print(f"config built: {len(proxies)} proxies")


if __name__ == '__main__':
    sys.exit(main())
