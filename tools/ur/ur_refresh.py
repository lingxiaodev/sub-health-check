#!/usr/bin/env python3
"""URnetwork 家宽节点池刷新任务（GitHub Actions 每 2 小时跑；本地也可跑）：
- 验活保留已有节点（出口 IP 不变、双 false、generate_204=204），死的删掉
- 缺额时按国家优先级轮询抽新节点补满 TARGET 个，不设每国配额；
  判定只看实测出口（能用优先于国家），节点按实测国家命名
- 生成 ur-clash.yaml / ur-v2ray.txt / ur-meta，推入 Cloudflare KV（sub-files）
状态：Actions 上经 Worker /ur-push 路由放 KV（ur-state，以 OIDC JWT 验权，
无需 CF_API_TOKEN）；本地放 600 权限文件。
只打印已脱敏摘要；auth_token 只存在状态与订阅文件里，绝不打印。
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402

try:
    sys.path.insert(0, os.path.expanduser('~/workspace/custom-connectors/custom.cloudflare/bin'))
    import cf  # noqa: E402
except Exception:
    cf = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'ur_state.json')
LOG_FILE = os.path.expanduser(
    '~/workspace/goals/free-proxy-node-subscription/hidden_files/ur-refresh.log')
ACCOUNT_ID = os.environ.get('CF_ACCOUNT_ID') or '90c6ef034b3c9e87e88efd9ef7a0271c'
NS = os.environ.get('CF_KV_NAMESPACE') or 'cfd2cf4db69743aaa2f18efdb117e30a'
KV_BASE = 'https://api.cloudflare.com/client/v4'
STATE_MODE = os.environ.get('UR_STATE_MODE', 'file')  # kv (Actions+CF token) | worker (Actions+OIDC) | file (local)
# worker 模式：状态与订阅文件经 sub-server 的 /ur-push 路由读写 KV，
# 以 GitHub Actions 的 OIDC JWT 验权，不需要 CF_API_TOKEN。
WORKER_BASE = os.environ.get('UR_WORKER_BASE') or 'https://sub.glmd.de5.net'
STATE_KEY = 'ur-state'
_OIDC = None

TARGET = 5
# 采样优先级（轮询取数，不设每国配额；能用优先于国家）
COUNTRIES = ['us', 'gb', 'de', 'jp', 'sg']
MAX_ATTEMPTS_PER_RUN = 15
MAX_ATTEMPTS_PER_COUNTRY = 5


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log_line(msg):
    print(f'[ur-refresh] {msg}')
    if STATE_MODE in ('kv', 'worker'):
        return
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(f'{now_iso()} {msg}\n')
    except Exception:
        pass


def cf_auth():
    tok = os.environ.get('CF_API_TOKEN')
    if tok:
        return 'Bearer ' + tok
    if cf is not None:
        return 'Bearer ' + cf._surrogate()
    raise SystemExit('no Cloudflare credential (CF_API_TOKEN or connector)')


def oidc_token(force_refresh=False):
    """GitHub Actions 的 OIDC JWT（workflow 需 id-token: write 权限）。
    token 有效期只有几分钟：推送阶段在启动 7 分钟后才到，必须能刷新。"""
    global _OIDC
    if _OIDC and not force_refresh:
        return _OIDC
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not url or not tok:
        raise SystemExit('no OIDC token request env (need id-token: write permission)')
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    q['audience'] = 'ur-push'
    full = urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))
    req = urllib.request.Request(full, headers={'Authorization': 'Bearer ' + tok})
    with urllib.request.urlopen(req, timeout=30) as r:
        _OIDC = json.load(r)['value']
    return _OIDC


def worker_get(key):
    url = WORKER_BASE + '/ur-push?key=' + urllib.parse.quote(key, safe='')
    for attempt in range(2):
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + oidc_token(force_refresh=(attempt > 0))})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 401 and attempt == 0:
                continue  # token 可能已过期，刷新一次重试
            raise SystemExit(f'worker GET {key} failed: {e.code} {e.read()[:160]!r}')


def worker_put(key, content):
    url = WORKER_BASE + '/ur-push'
    for attempt in range(2):
        body = json.dumps({'key': key, 'value': content}).encode('utf-8')
        req = urllib.request.Request(url, data=body,
                                     headers={'Authorization': 'Bearer ' + oidc_token(force_refresh=(attempt > 0)),
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue  # token 可能已过期，刷新一次重试
            return False, f'HTTP {e.code} {e.read()[:160]!r}'
        return bool(d.get('ok')), str(d)[:160]
    return False, 'unreachable'


def kv_get(key):
    if STATE_MODE == 'worker':
        return worker_get(key)
    cmd = ['curl', '-s', '--max-time', '30',
           f'{KV_BASE}/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NS}/values/{key}',
           '-H', 'Authorization: ' + cf_auth()]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return p.stdout


def kv_put(key, content):
    if STATE_MODE == 'worker':
        return worker_put(key, content)
    path = os.path.join(HERE, f'.kv-{key}')
    with open(path, 'w') as f:
        f.write(content)
    os.chmod(path, 0o600)
    cmd = ['curl', '-s', '--max-time', '60', '-X', 'PUT',
           f'{KV_BASE}/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NS}/values/{key}',
           '-H', 'Authorization: ' + cf_auth(),
           '-H', 'Content-Type: text/plain', '--data-binary', '@' + path]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    ok = '"success":true' in p.stdout.replace(' ', '')
    try:
        os.unlink(path)
    except Exception:
        pass
    return ok, p.stdout[:160]


def load_state():
    if STATE_MODE == 'kv':
        raw = kv_get(STATE_KEY)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            return []
    return []


def save_state(nodes):
    data = json.dumps(nodes, ensure_ascii=False, indent=1)
    if STATE_MODE in ('kv', 'worker'):
        ok, msg = kv_put(STATE_KEY, data)
        if not ok:
            raise SystemExit(f'state KV put failed: {msg}')
        return
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(data)
    os.chmod(STATE_FILE, 0o600)


def sanitize(s, default='x'):
    s = re.sub(r'[^A-Za-z0-9]+', '-', str(s or '')).strip('-')
    return s or default


def node_name(node):
    cc = (node['exit'].get('country_code') or 'xx').upper()
    city = sanitize(node['exit'].get('city'), 'unknown')
    isp = sanitize((node['exit'].get('isp') or '').split()[0], 'isp')
    return f'UR-{cc}-{city}-{isp}'


def proxy_check(node):
    """Returns (ok, reason). Re-verifies a kept node through its own proxy."""
    res = node['auth']['proxy_config_result']
    token = res.get('auth_token')
    host, port = node['socks_host'], node['socks_port']
    r1, r2 = extract.check_exit(token, host, port)
    if r1.get('status') != 'success':
        return False, f"ip-api fail: {r1.get('message') or r1.get('raw', '?')}"
    if r1.get('proxy') is not False or r1.get('hosting') is not False:
        return False, f"proxy={r1.get('proxy')} hosting={r1.get('hosting')}"
    if r1.get('query') != node['exit'].get('ip'):
        return False, f"IP changed: {node['exit'].get('ip')} -> {r1.get('query')}"
    want_cc = (node['exit'].get('country_code') or '').lower()
    if want_cc and (r1.get('countryCode') or '').lower() != want_cc:
        return False, f"country={r1.get('country')}"
    cmd = ['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '-m', '20',
           '--proxy', f'socks5h://{host}:{port}',
           '--proxy-user', f'{token}:x',
           'https://www.google.com/generate_204']
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stdout.strip() != '204':
        return False, f"generate_204={p.stdout.strip() or 'timeout'}"
    return True, 'ok'


def build_clash(nodes):
    proxies = []
    for node in nodes:
        res = node['auth']['proxy_config_result']
        proxies.append({
            'name': node_name(node), 'type': 'socks5',
            'server': node['socks_host'], 'port': node['socks_port'],
            'username': res.get('auth_token'), 'password': '',
        })
    names = [p['name'] for p in proxies]
    doc = {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule',
           'log-level': 'info', 'proxies': proxies,
           'proxy-groups': [{'name': '🚀 UR 家宽', 'type': 'select',
                             'proxies': names}],
           'rules': ['MATCH,🚀 UR 家宽']}
    return yaml_dump(doc)


def yaml_dump(value, indent=0):
    pad = '  ' * indent
    if isinstance(value, list):
        if not value:
            return '[]\n'
        out = ''
        for item in value:
            if isinstance(item, dict):
                sub = yaml_dump(item, indent + 1)
                out += pad + '- ' + sub[len(pad) + 2:]
            else:
                out += pad + '- ' + yaml_scalar(item) + '\n'
        return out
    if isinstance(value, dict):
        out = ''
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                out += pad + yaml_scalar(k) + ':\n' + yaml_dump(v, indent + 1)
            elif isinstance(v, (dict, list)):
                out += pad + yaml_scalar(k) + ': ' + ('[]' if isinstance(v, list) else '{}') + '\n'
            else:
                out += pad + yaml_scalar(k) + ': ' + yaml_scalar(v) + '\n'
        return out
    return yaml_scalar(value) + '\n'


def yaml_scalar(v):
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if s == '' or re.search(r'[:#\[\]{},&*!|>\'"%@`\s]', s) or s in ('true', 'false', 'null', '~'):
        return json.dumps(s, ensure_ascii=False)
    return s


def build_v2ray(nodes):
    lines = []
    for node in nodes:
        res = node['auth']['proxy_config_result']
        token = urllib.parse.quote(res.get('auth_token', ''), safe='-_.~')
        name = urllib.parse.quote(node_name(node), safe='-')
        lines.append(f"socks://{token}:@{node['socks_host']}:{node['socks_port']}#{name}")
    return '\n'.join(lines) + ('\n' if lines else '')


def main():
    kept = load_state()
    dropped, added = [], []
    # 1. 验活保留
    alive = []
    for node in kept:
        ok, reason = proxy_check(node)
        if ok:
            node['checked_at'] = now_iso()
            alive.append(node)
        else:
            cid = node['auth']['proxy_config_result'].get('client_id')
            if cid:
                extract.remove_client(cid)
            dropped.append({'name': node_name(node), 'reason': reason})
    if len(alive) > TARGET:
        for node in alive[TARGET:]:
            cid = node['auth']['proxy_config_result'].get('client_id')
            if cid:
                extract.remove_client(cid)
            dropped.append({'name': node_name(node), 'reason': 'over target'})
        alive = alive[:TARGET]
    # 2. 缺额补抽：按国家优先级轮询，不设每国配额
    tried_ids = {n['provider']['client_id'] for n in alive}
    attempts_total = 0
    for _pass in range(3):
        if len(alive) >= TARGET or attempts_total >= MAX_ATTEMPTS_PER_RUN:
            break
        progressed = False
        for cc in COUNTRIES:
            if len(alive) >= TARGET or attempts_total >= MAX_ATTEMPTS_PER_RUN:
                break
            providers = extract.find_providers(cc, count=20, exclude_ids=tried_ids)
            if not providers:
                log_line(f'{cc}: provider list empty')
                continue
            winners, attempts = extract.extract_round(
                providers, max_attempts=MAX_ATTEMPTS_PER_COUNTRY, want=1,
                exclude_ids=tried_ids)
            attempts_total += len(attempts)
            for a in attempts:
                tried_ids.add(a.get('provider_client_id', ''))
            if winners:
                w = winners[0]
                w['checked_at'] = now_iso()
                alive.append(w)
                added.append({'name': node_name(w),
                              'exit': w['exit']['ip'], 'city': w['exit']['city'],
                              'country': w['exit'].get('country'),
                              'isp': w['exit']['isp']})
                progressed = True
            else:
                log_line(f"{cc}: no winner this round: "
                         + '; '.join(f"#{a.get('n')} {a.get('reason')}" for a in attempts))
        if not progressed:
            break
    # 3. 保存状态 + 生成订阅文件 + 推 KV
    save_state(alive)
    clash = build_clash(alive)
    v2ray = build_v2ray(alive)
    meta = {'updated': int(time.time()), 'count': len(alive),
            'nodes': [{'country': (n['exit'].get('country_code') or '').lower(),
                       'ip': n['exit'].get('ip'), 'city': n['exit'].get('city'),
                       'region': n['exit'].get('region'),
                       'isp': n['exit'].get('isp'),
                       'checked_at': n.get('checked_at')} for n in alive]}
    results = {}
    for key, content in (('ur-clash.yaml', clash), ('ur-v2ray.txt', v2ray),
                         ('ur-meta', json.dumps(meta, ensure_ascii=False))):
        ok, msg = kv_put(key, content)
        results[key] = 'OK' if ok else msg
    pushed = all(v == 'OK' for v in results.values())
    summary = {'kept': len(alive) - len(added), 'added': added,
               'dropped': dropped, 'pushed': pushed, 'push': results,
               'nodes': [{'name': node_name(n), 'ip': n['exit'].get('ip'),
                          'city': n['exit'].get('city'),
                          'country': n['exit'].get('country'),
                          'isp': n['exit'].get('isp')} for n in alive]}
    log_line('kept=%d added=%d dropped=%d pushed=%s nodes=%s' % (
        summary['kept'], len(added), len(dropped), pushed,
        ','.join(node_name(n) for n in alive) or '-'))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if not alive:
        print('WARN: subscription is EMPTY (no live UR nodes)', file=sys.stderr)
        return 2
    if not pushed:
        print('WARN: KV push failed', file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.exit(main())
