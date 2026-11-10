"""Optional public X metrics GET. No redirects, writes, login or private insights."""
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit

CANONICAL = {'likes': 'like_count', 'reposts': 'retweet_count', 'replies': 'reply_count', 'views': 'impression_count'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_id(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != 'https' or parsed.hostname not in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com') or parsed.username or parsed.password or port not in (None, 443):
        return None
    match = re.fullmatch(r'/[A-Za-z0-9_]+/status/(\d+)/?', parsed.path)
    return match[1] if match else None


def collect(url, cfg, *, dry_run=False, mock=None, transport=None):
    empty = {k: 'unverified' for k in CANONICAL}
    settings = cfg['ops']['measure']
    pid = public_id(url)
    key = os.environ.get(settings['x_bearer_env'], '')
    if dry_run or mock is not None or settings['x_public'] is not True or not key or not pid:
        return empty
    from .guards import register_secret
    register_secret(key)
    request = urllib.request.Request('https://api.x.com/2/tweets/' + pid + '?tweet.fields=public_metrics',
                                     headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
    try:
        opener = transport or urllib.request.build_opener(NoRedirect()).open
        with opener(request, timeout=10) as response:
            if response.geturl() != request.full_url or response.status != 200: return empty
            body = response.read(131073)
        if len(body) > 131072: return empty
        data = json.loads(body)['data']
        if data.get('id') != pid or not isinstance(data.get('public_metrics'), dict): return empty
        raw = data['public_metrics']
        if any(k in raw and (type(raw[k]) is not int or raw[k] < 0) for k in CANONICAL.values()): return empty
        return {k: raw.get(v, 'unverified') for k, v in CANONICAL.items()}
    except Exception:
        # Provider errors and credentials must never reach logs or user output.
        return empty
