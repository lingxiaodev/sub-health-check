#!/usr/bin/env python3
"""URnetwork pinned-provider extraction helpers + manual one-round CLI.
Prints REDACTED summaries only. Secrets (api key, auth tokens) are read
from files and never printed."""
import json, os, subprocess, time

WORK = os.path.dirname(os.path.abspath(__file__))
ENV = os.environ.get('URNETWORK_ENV_FILE') or os.path.join(
    os.path.dirname(WORK), 'urnetwork.env')
BASE = 'https://api.bringyour.com'
MAX_ATTEMPTS = 5

def load_key():
    """API key from the environment first (GitHub Actions), else the local
    600-permission env file. Never print the value."""
    env_key = os.environ.get('URNETWORK_API_KEY')
    if env_key:
        return env_key
    for line in open(ENV):
        line = line.strip()
        if line.startswith('URNETWORK_API_KEY='):
            return line.split('=', 1)[1]
    raise SystemExit('no api key')

KEY = load_key()
_last_create = 0.0

def api(method, path, body=None):
    cmd = ['curl', '-sS', '-m', '30', '-X', method, BASE + path,
           '-H', f'Authorization: Bearer {KEY}',
           '-H', 'Content-Type: application/json']
    if body is not None:
        cmd += ['-d', json.dumps(body)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {'_raw': p.stdout[:300], '_stderr': p.stderr[:300]}

def list_clients():
    """账号上当前活跃的托管代理。注意：/network/clients 返回的是网络对端
    （常为空），托管代理在 /network/proxies 下，同样是 {clients: [...]}。"""
    d = api('GET', '/network/proxies')
    if isinstance(d, dict):
        return d.get('clients') or []
    if isinstance(d, list):
        return d
    return []


def remove_client(client_id):
    return api('POST', '/network/remove-client', {'client_id': client_id})

def country_location_id(country_code):
    """country_code like 'us' -> location_id (cached in locations.json)."""
    def _find(locs):
        if isinstance(locs, dict):
            locs = locs.get('locations', [])
        for loc in locs or []:
            if isinstance(loc, dict) and loc.get('country_code', '').lower() == country_code.lower():
                return loc['location_id']
        return None
    path = os.path.join(WORK, 'locations.json')
    if os.path.exists(path):
        got = _find(json.load(open(path)))
        if got:
            return got
    # refresh cache
    locs = api('GET', '/network/provider-locations')
    if isinstance(locs, (list, dict)):
        json.dump(locs, open(path, 'w'))
        return _find(locs)
    return None

def find_providers(country_code, count=20, exclude_ids=None):
    loc_id = country_location_id(country_code)
    if not loc_id:
        return []
    body = {'specs': [{'location_id': loc_id}], 'count': count}
    if exclude_ids:
        body['exclude_client_ids'] = list(exclude_ids)
    d = api('POST', '/network/find-providers2', body)
    providers = d.get('providers', []) if isinstance(d, dict) else []
    providers.sort(key=lambda p: p.get('estimated_bytes_per_second', 0), reverse=True)
    return providers

def _curl_ipapi(token, host, port):
    cmd = ['curl', '-sS', '-m', '25', '--proxy', f'socks5h://{host}:{port}',
           '--proxy-user', f'{token}:x',
           'http://ip-api.com/json/?fields=status,message,country,countryCode,regionName,city,isp,org,as,proxy,hosting,query,lat,lon']
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {'status': 'fail', 'raw': (p.stdout + p.stderr)[:200]}

def check_exit(token, host, port):
    """Two ip-api queries ~5s apart; one retry per query on timeout/empty."""
    results = []
    for attempt in range(2):
        r = _curl_ipapi(token, host, port)
        if r.get('status') in (None, 'fail') and not r.get('query'):
            time.sleep(3)
            r = _curl_ipapi(token, host, port)
        results.append(r)
        if attempt == 0:
            time.sleep(5)
    return results

DC_WORDS = ['datacenter', 'data center', 'hosting', 'vps', 'colocation',
            'amazon', 'digitalocean', 'linode', 'vultr', 'ovh', 'hetzner',
            'google cloud', 'microsoft azure', 'alibaba', 'tencent cloud',
            'akamai', 'cloudflare']

def judge(provider_loc, r1, r2):
    """Returns (ok, reason). Judges the MEASURED exit only: the provider's
    claimed location is just a sampling hint, not a requirement (usability
    matters more than the country label). The exit must be a real
    residential IP: double false, real broadband ISP, stable across the
    two queries, ISP/AS consistent."""
    if r1.get('status') != 'success':
        return False, f"ip-api fail: {r1.get('message') or r1.get('raw', '?')}"
    if r1.get('proxy') is not False or r1.get('hosting') is not False:
        return False, f"proxy={r1.get('proxy')} hosting={r1.get('hosting')}"
    if r2.get('query') != r1.get('query'):
        return False, f"IP unstable: {r1.get('query')} vs {r2.get('query')}"
    isp = (r1.get('isp') or '').lower()
    if any(w in isp for w in DC_WORDS):
        return False, f"ISP looks datacenter: {r1.get('isp')}"
    as_field = (r1.get('as') or '').lower()
    org = (r1.get('org') or '').lower()
    isp_first = isp.split()[0] if isp.split() else ''
    if isp_first and len(isp_first) > 3:
        if isp_first not in as_field and isp_first not in org:
            return False, f"ISP/AS mismatch: isp={r1.get('isp')} as={r1.get('as')} org={r1.get('org')}"
    return True, 'ok'

def auth_client(provider_client_id):
    """Pin one provider. Throttled to >=12s between creates (5/min cap)."""
    global _last_create
    wait = 12 - (time.time() - _last_create)
    if wait > 0:
        time.sleep(wait)
    auth = api('POST', '/network/auth-client', {
        'proxy_config': {'initial_device_state': {'location': {
            'connect_location_id': {'client_id': provider_client_id}}}}})
    _last_create = time.time()
    return auth

def extract_one(provider):
    """Try one provider. Returns (winner_dict_or_None, attempt_record)."""
    loc = provider.get('location', {})
    base = {'provider_client_id': provider['client_id'],
            'provider_city': loc.get('city'), 'provider_region': loc.get('region'),
            'provider_country': loc.get('country_code'),
            'speed': provider.get('estimated_bytes_per_second')}
    auth = auth_client(provider['client_id'])
    if 'error' in auth or 'proxy_config_result' not in auth:
        base.update(result='auth-error', reason=str(auth.get('error', auth))[:200])
        return None, base
    res = auth.get('proxy_config_result', {})
    token = res.get('auth_token')
    created_client_id = res.get('client_id')
    socks_url = res.get('socks_proxy_url') or ''
    if socks_url:
        hp = socks_url.replace('socks5h://', '').replace('socks5://', '')
        host, port = hp.rsplit(':', 1)
        port = int(port)
    else:
        host, port = 'crisp.bringyour.com', 7117
    if not token:
        base.update(result='no-auth-token', reason='auth_token missing in response')
        if created_client_id:
            remove_client(created_client_id)
        return None, base
    time.sleep(3)
    r1, r2 = check_exit(token, host, port)
    ok, reason = judge(loc, r1, r2)
    exit_info = {'ip': r1.get('query'), 'city': r1.get('city'),
                 'region': r1.get('regionName'),
                 'country': r1.get('country'),
                 'country_code': (r1.get('countryCode') or '').lower(),
                 'isp': r1.get('isp'),
                 'org': r1.get('org'), 'as': r1.get('as'),
                 'proxy': r1.get('proxy'), 'hosting': r1.get('hosting')}
    base.update(socks_host=host, socks_port=port, exit=exit_info,
                result='PASS' if ok else 'FAIL', reason=reason)
    if ok:
        winner = {'provider': provider, 'auth': auth, 'exit': exit_info,
                  'socks_host': host, 'socks_port': port}
        return winner, base
    if created_client_id:
        remove_client(created_client_id)
    return None, base

def extract_round(providers, max_attempts=MAX_ATTEMPTS, want=1, exclude_ids=None):
    """Try providers in order (already speed-sorted). Returns (winners, attempts)."""
    winners, attempts = [], []
    tried = set(exclude_ids or [])
    for provider in providers:
        if len(winners) >= want or len(attempts) >= max_attempts:
            break
        if provider['client_id'] in tried:
            continue
        tried.add(provider['client_id'])
        winner, att = extract_one(provider)
        att['n'] = len(attempts) + 1
        attempts.append(att)
        if winner:
            winners.append(winner)
        # platform limit/rate error -> stop the round
        if att.get('result') == 'auth-error' and (
                'limit' in str(att.get('reason', '')).lower()
                or 'rate' in str(att.get('reason', '')).lower()):
            break
    return winners, attempts

def main():
    providers = json.load(open(os.path.join(WORK, 'providers_sorted.json')))
    winners, attempts = extract_round(providers)
    summary = {'attempts': attempts,
               'winner': ({'exit': winners[0]['exit'], 'socks_host': winners[0]['socks_host'],
                           'socks_port': winners[0]['socks_port']} if winners else None)}
    if winners:
        json.dump(winners[0], open(os.path.join(WORK, 'winner.json'), 'w'))
    json.dump(summary, open(os.path.join(WORK, 'extract_summary.json'), 'w'))
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
