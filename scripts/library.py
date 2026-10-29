"""Prepare signed TaiXu releases; binary packages never enter Git history."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile
from gitee_mirror import publish_assets as publish_gitee, SMALL_LIMIT, DEFAULT_REPOSITORY
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 2 * 1024**3
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[0-9A-Za-z.-]+)?")


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def key(path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def sign(catalog, private):
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    expected = base64.b64decode((ROOT / 'public-key.txt').read_text(encoding='utf-8').strip())
    if public != expected:
        raise ValueError('Signing key does not match public-key.txt')
    return base64.b64encode(private.sign(catalog)) + b'\n'


def generate_key(destination):
    private = Ed25519PrivateKey.generate()
    destination = destination.resolve()
    if destination.is_relative_to(ROOT):
        raise ValueError('Private key must be outside repository')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(destination, 0o600)
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    with (ROOT / 'public-key.txt').open('x') as stream:
        stream.write(base64.b64encode(public).decode() + '\n')


def prepare(package, private_path, repository, chunk_mib):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    chunk_size = chunk_mib * 1024**2
    if not 1 <= chunk_mib <= 1024:
        raise ValueError('chunk-mib must be between 1 and 1024')
    with zipfile.ZipFile(package) as archive:
        manifests = [i for i in archive.infolist() if i.filename == 'manifest.json']
        if len(manifests) != 1 or manifests[0].file_size > 1024**2:
            raise ValueError('Exactly one bounded root manifest.json required')
        manifest = json.loads(archive.read(manifests[0]))
        schema = manifest.get('schemaVersion', 1)
        expected = manifest.get('plugin', {}).get('integrity', {})
        seen = set()
        for entry in archive.infolist():
            if entry.is_dir() or entry.filename == 'manifest.json':
                continue
            if not entry.filename.startswith('payload/') or '..' in entry.filename.split('/') or '\\' in entry.filename:
                raise ValueError('Invalid package path')
            relative = entry.filename[len('payload/'):]
            if relative in seen or (schema == 2 and relative not in expected):
                raise ValueError('Unexpected/duplicate payload file')
            seen.add(relative)
            h = hashlib.sha256()
            with archive.open(entry) as stream:
                for block in iter(lambda: stream.read(1024**2), b''):
                    h.update(block)
            if schema == 2 and h.hexdigest() != expected[relative]:
                raise ValueError('Payload checksum mismatch')
        if schema == 2 and seen != set(expected):
            raise ValueError('Payload file set mismatch')
    tool, version = manifest['id'], manifest['version']
    spec = manifest['plugin'] if schema == 2 else dict(id='legacy.tool-' + tool, execution='PROCESS', api=dict(minimum=1, maximum=1))
    if schema not in (1, 2) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,63}', tool):
        raise ValueError('A v1/v2 package with a valid tool id is required')
    if not SEMVER.fullmatch(version) or spec['execution'] not in ('PROCESS', 'WEB'):
        raise ValueError('Stable SemVer PROCESS/WEB package required')
    if schema == 2 and (not re.fullmatch(r'[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+', spec['id']) or spec['id'].startswith(('taixu.', 'legacy.'))):
        raise ValueError('Invalid/reserved plugin identity')
    if not 0 < package.stat().st_size <= 8 * 1024**3:
        raise ValueError('Package must be between 1 byte and 8 GiB')
    if package.stat().st_size >= LIMIT and (package.stat().st_size + chunk_size - 1) // chunk_size > 64:
        raise ValueError('Too many parts; increase chunk-mib to produce at most 64 parts')
    tag = f'{tool}-v{version}'
    output = ROOT / 'dist' / tag
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    plugin = next((p for p in catalog['plugins'] if p['pluginId'] == spec['id']), None)
    if plugin:
        if plugin['toolId'] != tool or plugin['execution'] != spec['execution']:
            raise ValueError('Cannot change package identity/type')
        if any(r['version'] == version for r in plugin['releases']):
            raise ValueError('Version already exists; increment version')
        precedence = lambda v: tuple(map(int, v.split('+')[0].split('.')))
        if plugin['releases'] and precedence(version) <= max(precedence(r['version']) for r in plugin['releases']):
            raise ValueError('Version must increase')
    else:
        if any(p['toolId'] == tool for p in catalog['plugins']):
            raise ValueError('Tool identity conflict')
        plugin = dict(pluginId=spec['id'], toolId=tool, name=manifest['name'], description=manifest['description'], execution=spec['execution'], releases=[])
        catalog['plugins'].append(plugin)
    parts = []
    output.mkdir(parents=True, exist_ok=False)
    # Stream chunks: memory use stays constant even for multi-gigabyte packages.
    with package.open('rb') as source:
        remaining = package.stat().st_size
        index = 1
        while remaining:
            name = f'{tool}-{version}.txplugin'
            if package.stat().st_size >= LIMIT:
                name += f'.part{index:03}'
            target = output / name
            amount = min(chunk_size, remaining) if package.stat().st_size >= LIMIT else remaining
            with target.open('xb') as stream:
                left = amount
                while left:
                    block = source.read(min(1024**2, left))
                    if not block:
                        raise ValueError('Package changed during reading')
                    stream.write(block)
                    left -= len(block)
            parts.append(dict(url=f'https://github.com/{repository}/releases/download/{tag}/{name}', size=amount, sha256=digest(target)))
            remaining -= amount
            index += 1
    release = dict(version=version, channel='stable', platform='linux-arm64', packageSchema=schema, pluginApi=spec['api'], size=package.stat().st_size, sha256=digest(package), parts=parts)
    plugin['releases'].append(release)
    catalog['revision'] += 1
    content = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode()
    if len(content) > 1024**2:
        raise ValueError('Catalog exceeds 1 MiB')
    signature = sign(content, key(private_path))
    (output / 'catalog.json').write_bytes(content)
    (output / 'catalog.json.sig').write_bytes(signature)
    (output / 'release.json').write_text(json.dumps(dict(repository=repository, tag=tag, assets=[Path(p['url']).name for p in parts]), indent=2))
    print(f'Prepared {output}. Review, then run publish with this directory.')
    return output


def publish(directory, private_path=None, github_only=False):
    directory = directory.resolve()
    info = json.loads((directory / 'release.json').read_text(encoding='utf-8'))
    # Refuse stale prepared catalogs before performing a remote mutation.
    current = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    candidate = (directory / 'catalog.json').read_bytes()
    if json.loads(candidate)['revision'] != current['revision'] + 1:
        raise ValueError('Stale prepared release; prepare again against current catalog')
    key_bytes = base64.b64decode((ROOT / 'public-key.txt').read_text(encoding='utf-8').strip())
    serialization.load_der_public_key(key_bytes).verify(base64.b64decode((directory / 'catalog.json.sig').read_bytes()), candidate)
    for name in info['assets']:
        if Path(name).name != name:
            raise ValueError('Invalid asset name')
        if not (directory / name).is_file():
            raise ValueError('Missing release asset')
    expected = [part for plugin in json.loads(candidate)['plugins'] for release in plugin['releases'] for part in release['parts'] if f"/download/{info['tag']}/" in part['url']]
    if {Path(p['url']).name for p in expected} != set(info['assets']):
        raise ValueError('Asset list differs from signed catalog')
    for part in expected:
        asset = directory / Path(part['url']).name
        if asset.stat().st_size != part['size'] or digest(asset) != part['sha256']:
            raise ValueError('Asset changed after signing')
    if sum(p['size'] for p in expected) > SMALL_LIMIT:
        raise ValueError('This library only publishes small plugins (at most 96 MiB); large packages must stay local')
    if not github_only and sum(p['size'] for p in expected) <= SMALL_LIMIT:
        private_path = private_path or Path.home() / '.codex/keys/taixu-library-signing.pem'
        private = key(private_path)
        sign(candidate, private)  # Check the signing key before any upload.
        mirrors = publish_gitee(directory, info['tag'], expected)
        updated = json.loads(candidate)
        for plugin in updated['plugins']:
            for release in plugin['releases']:
                for part in release['parts']:
                    if part in expected and Path(part['url']).name in mirrors:
                        part.setdefault('mirrors', {})['GITEE'] = mirrors[Path(part['url']).name]
        candidate = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode()
        (directory / 'catalog.json').write_bytes(candidate)
        (directory / 'catalog.json.sig').write_bytes(sign(candidate, private))
    assets = [str(directory / name) for name in info['assets']]
    subprocess.run(['gh', 'release', 'create', info['tag'], *assets, '--repo', info['repository'], '--title', info['tag'], '--notes', 'TaiXu plugin package. See catalog.json for checksums and compatibility.', '--latest=false'], check=True)
    # The public index is updated only after every asset has been uploaded/published.
    (ROOT / 'catalog.json').write_bytes(candidate)
    (ROOT / 'catalog.json.sig').write_bytes((directory / 'catalog.json.sig').read_bytes())
    if not github_only and sum(p['size'] for p in expected) <= SMALL_LIMIT:
        merge_gitee_catalog(updated['plugins'], private)
    print('Release published. Review and commit catalog.json + catalog.json.sig, then push to expose the update.')


def sync_gitee(private_path):
    """Backfill only published small packages; no GitHub release mutation."""
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    private = key(private_path)
    sign((ROOT / 'catalog.json').read_bytes(), private)
    updated = False
    for plugin in catalog['plugins']:
        for release in plugin['releases']:
            if release['size'] > SMALL_LIMIT or all('GITEE' in p.get('mirrors', {}) for p in release['parts']):
                continue
            tag = f"{plugin['toolId']}-v{release['version']}"
            directory = ROOT / 'dist' / tag
            for part in release['parts']:
                file = directory / Path(part['url']).name
                if not file.is_file() or file.stat().st_size != part['size'] or digest(file) != part['sha256']:
                    raise ValueError(f'Missing/changed local release asset: {file}')
            mirrors = publish_gitee(directory, tag, release['parts'])
            for part in release['parts']:
                part.setdefault('mirrors', {})['GITEE'] = mirrors[Path(part['url']).name]
            updated = True
    if updated:
        catalog['revision'] += 1
        content = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode()
        signature = sign(content, private)
        (ROOT / 'catalog.json').write_bytes(content)
        (ROOT / 'catalog.json.sig').write_bytes(signature)
        merge_gitee_catalog(catalog['plugins'], private)
    print('Gitee small-plugin sync complete. Push the signed index to both remotes.' if updated else 'No published small plugins need syncing.')


def merge_gitee_catalog(plugins, private):
    """Gitee publishes an independent signed index: never advertise a missing GitHub asset."""
    path = ROOT / 'catalog-gitee.json'
    catalog = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(schemaVersion=1, revision=0, plugins=[])
    for source in plugins:
        versions = []
        for release in source['releases']:
            if release['size'] <= SMALL_LIMIT and all('GITEE' in p.get('mirrors', {}) for p in release['parts']):
                mirrored = json.loads(json.dumps(release))
                for part in mirrored['parts']: part['url'] = part['mirrors']['GITEE']
                versions.append(mirrored)
        if not versions: continue
        target = next((p for p in catalog['plugins'] if p['pluginId'] == source['pluginId']), None)
        if target is None:
            target = dict(source, releases=[])
            catalog['plugins'].append(target)
        if target['toolId'] != source['toolId'] or target['execution'] != source['execution']:
            raise ValueError('Gitee plugin identity conflict')
        for version in versions:
            previous = next((r for r in target['releases'] if r['version'] == version['version']), None)
            if previous and previous['sha256'] != version['sha256']:
                raise ValueError('Cannot replace an existing Gitee package version')
            if previous is None: target['releases'].append(version)
    catalog['revision'] += 1
    content = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode()
    signature = sign(content, private)
    path.write_bytes(content)
    (ROOT / 'catalog-gitee.json.sig').write_bytes(signature)


def publish_gitee_only(directory, private_path):
    info = json.loads((directory / 'release.json').read_text(encoding='utf-8'))
    candidate = (directory / 'catalog.json').read_bytes()
    private = key(private_path)
    public = private.public_key()
    public.verify(base64.b64decode((directory / 'catalog.json.sig').read_bytes()), candidate)
    sign(candidate, private)
    plugins = json.loads(candidate)['plugins']
    chosen = [(p, r) for p in plugins for r in p['releases'] if f"{p['toolId']}-v{r['version']}" == info['tag']]
    if len(chosen) != 1: raise ValueError('Missing prepared release')
    plugin, release = chosen[0]
    if release['size'] > SMALL_LIMIT: raise ValueError('Large plugins are GitHub-only')
    for part in release['parts']:
        asset = directory / Path(part['url']).name
        if asset.stat().st_size != part['size'] or digest(asset) != part['sha256']:
            raise ValueError('Prepared asset changed')
    mirrors = publish_gitee(directory, info['tag'], release['parts'])
    for part in release['parts']: part.setdefault('mirrors', {})['GITEE'] = mirrors[Path(part['url']).name]
    merge_gitee_catalog([dict(plugin, releases=[release])], private)
    print('Gitee release verified; catalog-gitee.json + signature updated. Commit and push to both remotes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    k = sub.add_parser('keygen'); k.add_argument('--private-key', type=Path, required=True)
    p = sub.add_parser('prepare'); p.add_argument('package', type=Path); p.add_argument('--private-key', type=Path, required=True)
    p.add_argument('--repo', default='wkbin/taixu-library'); p.add_argument('--chunk-mib', type=int, default=512)
    u = sub.add_parser('publish'); u.add_argument('directory', type=Path)
    u.add_argument('--private-key', type=Path); u.add_argument('--github-only', action='store_true')
    m = sub.add_parser('sync-gitee'); m.add_argument('--private-key', type=Path, default=Path.home() / '.codex/keys/taixu-library-signing.pem')
    g = sub.add_parser('publish-gitee'); g.add_argument('directory', type=Path)
    g.add_argument('--private-key', type=Path, default=Path.home() / '.codex/keys/taixu-library-signing.pem')
    args = parser.parse_args()
    if args.command == 'keygen': generate_key(args.private_key)
    elif args.command == 'prepare': prepare(args.package, args.private_key, args.repo, args.chunk_mib)
    elif args.command == 'sync-gitee': sync_gitee(args.private_key)
    elif args.command == 'publish-gitee': publish_gitee_only(args.directory, args.private_key)
    else: publish(args.directory, args.private_key, args.github_only)


if __name__ == '__main__':
    main()
