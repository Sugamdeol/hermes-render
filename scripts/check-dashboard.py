"""Check the affected tabs locally without printing credentials or response data."""
import json
import re
import urllib.error
import urllib.request

PATHS = ('/api/analytics/usage?days=30', '/api/analytics/models?days=30',
         '/api/dashboard/plugins/hub', '/api/skills')


def check(base='http://127.0.0.1:9119'):
    with urllib.request.urlopen(base+'/', timeout=15) as response:
        html = response.read(2 * 1024 * 1024).decode('utf-8', errors='replace')
    match = re.search(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"\s]+)"', html)
    if not match:
        raise RuntimeError('Dashboard HTML did not contain its session token; refresh after startup.')
    results = []
    for path in PATHS:
        request = urllib.request.Request(base+path, headers={'X-Hermes-Session-Token':match[1]})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('response too large')
                json.loads(raw)
            results.append({'path':path, 'ok':True, 'status':200})
        except urllib.error.HTTPError as exc:
            exc.close()
            results.append({'path':path, 'ok':False, 'status':exc.code})
        except (OSError, ValueError):
            results.append({'path':path, 'ok':False, 'status':'unavailable or invalid JSON'})
    return results
