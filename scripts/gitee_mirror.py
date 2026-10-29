"""Gitee small-package mirror. Tokens stay in the environment and HTTP headers."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

SMALL_LIMIT = 96 * 1024**2
DEFAULT_REPOSITORY = 'wang_ke_bin/taixu-library'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the publishing token to another host.


class GiteeClient:
    def __init__(self, repository=DEFAULT_REPOSITORY):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid Gitee repository')
        self.repository = repository
        self.prefix = f'/api/v5/repos/{repository}'
        self.token = os.environ.get('GITEE_ACCESS_TOKEN', '')
        if not self.token:
            raise ValueError('Set GITEE_ACCESS_TOKEN locally; do not paste it into chat or commit it')

    def request(self, method, suffix, data=None):
        content = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request('https://gitee.com' + self.prefix + suffix, data=content, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                body = response.read(1024**2 + 1)
                if len(body) > 1024**2:
                    raise ValueError('Gitee API response too large')
                return json.loads(body)
        except urllib.error.HTTPError as failure:
            if method == 'GET' and failure.code == 404:
                return None
            raise RuntimeError(f'Gitee API failed: HTTP {failure.code}') from None

    def release(self, tag):
        release = self.request('GET', '/releases/tags/' + urllib.parse.quote(tag, safe=''))
        if release is None:
            release = self.request('POST', '/releases', dict(tag_name=tag, name=tag,
                body='TaiXu small plugin mirror; verify checksums using the signed catalog.', target_commitish='main', prerelease=False))
        return release

    def upload(self, release_id, file):
        if not file.name.isascii() or any(c in file.name for c in '\r\n"'):
            raise ValueError('Invalid upload filename')
        boundary = 'taixu-' + uuid.uuid4().hex
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode()
        tail = f'\r\n--{boundary}--\r\n'.encode()
        connection = http.client.HTTPSConnection('gitee.com', timeout=120)
        try:
            connection.putrequest('POST', f'{self.prefix}/releases/{int(release_id)}/attach_files')
            connection.putheader('Authorization', 'Bearer ' + self.token)
            connection.putheader('Content-Type', f'multipart/form-data; boundary={boundary}')
            connection.putheader('Content-Length', str(len(head) + file.stat().st_size + len(tail)))
            connection.endheaders()
            connection.send(head)
            sent = 0
            with file.open('rb') as stream:
                for block in iter(lambda: stream.read(1024**2), b''):
                    connection.send(block)
                    sent += len(block)
                    if sent % (8 * 1024**2) == 0:
                        print(f'Gitee upload {file.name}: {sent // 1024**2} / {file.stat().st_size // 1024**2} MiB', flush=True)
            connection.send(tail)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise RuntimeError(f'Gitee upload failed: HTTP {response.status}')
            body = response.read(1024**2 + 1)
            if len(body) > 1024**2:
                raise ValueError('Gitee upload response too large')
            return json.loads(body)
        finally:
            connection.close()


def download_url(attachment):
    value = attachment.get('browser_download_url') or attachment.get('url')
    uri = urllib.parse.urlparse(value or '')
    if uri.scheme != 'https' or uri.hostname != 'gitee.com' or uri.username or uri.password or uri.query or uri.fragment:
        raise ValueError('Unexpected Gitee attachment download URL')
    return value


def remote_hash(url):
    digest = hashlib.sha256()
    count = 0
    # Public downloads never include a publisher token, even across redirects.
    with urllib.request.urlopen(url, timeout=120) as response:
        if urllib.parse.urlparse(response.url).scheme != 'https':
            raise ValueError('Insecure Gitee attachment redirect')
        for block in iter(lambda: response.read(1024**2), b''):
            count += len(block)
            if count > SMALL_LIMIT:
                raise ValueError('Gitee attachment exceeds small-package limit')
            digest.update(block)
            if count % (8 * 1024**2) == 0:
                print(f'Gitee checksum download: {count // 1024**2} MiB', flush=True)
    return count, digest.hexdigest()


def publish_assets(directory, tag, parts, repository=DEFAULT_REPOSITORY, client=None):
    """Existing attachments are rehashed before reuse; never overwrite released bytes."""
    if sum(p['size'] for p in parts) > SMALL_LIMIT or len(parts) != 1:
        return {}  # Large packages stay on GitHub.
    client = client or GiteeClient(repository)
    release = client.release(tag)
    attachments = client.request('GET', f"/releases/{int(release['id'])}/attach_files") or []
    result = {}
    for part in parts:
        name = Path(part['url']).name
        matches = [a for a in attachments if (a.get('name') or a.get('file_name')) == name]
        if len(matches) > 1:
            raise ValueError('Ambiguous existing Gitee attachment')
        attachment = matches[0] if matches else client.upload(release['id'], directory / name)
        url = download_url(attachment)
        print(f'Gitee verifying downloaded checksum: {name}', flush=True)
        if remote_hash(url) != (part['size'], part['sha256']):
            raise ValueError('Uploaded/existing Gitee asset checksum mismatch')
        result[name] = url
    return result
