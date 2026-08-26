from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE = 'https://csv-cleaner-app-uc0p.onrender.com/'
ROUTES = [
    '', '_stcore/health', '_stcore/host-config', '_stcore/allowed-message-origins',
    'healthz', 'health', 'api', 'api/', 'api/clean', 'api/clean/', 'api/health',
    'openapi.json', 'docs', 'docs/', 'swagger.json', 'robots.txt', 'sitemap.xml',
    'favicon.png', 'static/js/index.dZusM_HY.js',
]
results = []
for route in ROUTES:
    url = urljoin(BASE, route)
    try:
        response = requests.get(url, timeout=20, allow_redirects=False, stream=True)
        body = b''
        for chunk in response.iter_content(chunk_size=4096):
            body += chunk
            if len(body) >= 65536:
                break
        results.append({
            'route': '/' + route,
            'status': response.status_code,
            'content_type': response.headers.get('content-type', ''),
            'content_length': response.headers.get('content-length', ''),
            'location': response.headers.get('location', ''),
            'sample': body[:500].decode('utf-8', errors='replace'),
        })
    except Exception as exc:
        results.append({'route': '/' + route, 'error': str(exc)})
Path('/home/ubuntu/contact_directory_desktop/public_route_probe.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
for item in results:
    print(item.get('route'), item.get('status', 'ERROR'), item.get('content_type', ''), item.get('location', ''))
