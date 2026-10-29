import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import library


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.patch = patch.object(library, 'ROOT', self.root)
        self.patch.start()
        self.private = self.base / 'signing.pem'
        library.generate_key(self.private)
        (self.root / 'catalog.json').write_text(json.dumps(dict(schemaVersion=1, revision=1, plugins=[])))
        self.package = self.base / 'hello.txplugin'
        manifest = dict(schemaVersion=2, id='hello-plugin', version='1.0.0', name='太墟插件', description='离线测试',
                        plugin=dict(id='example.hello', execution='PROCESS', api=dict(minimum=1, maximum=1), integrity={}))
        with zipfile.ZipFile(self.package, 'w') as z:
            z.writestr('manifest.json', json.dumps(manifest))

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def prepare(self):
        return library.prepare(self.package, self.private, 'wkbin/taixu-library', 1)

    def test_single_file_signature_and_no_index_mutation(self):
        out = self.prepare()
        catalog = (out / 'catalog.json').read_bytes()
        public = library.serialization.load_der_public_key(base64.b64decode((self.root / 'public-key.txt').read_text(encoding='utf-8')))
        public.verify(base64.b64decode((out / 'catalog.json.sig').read_bytes()), catalog)
        self.assertEqual(len(json.loads(catalog)['plugins'][0]['releases'][0]['parts']), 1)
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))['revision'], 1)

    def test_split_reassembles_exact_bytes(self):
        payload = b'x' * (2 * 1024**2 + 100)
        manifest = dict(schemaVersion=2, id='hello-plugin', version='1.0.0', name='太墟插件', description='离线测试',
                        plugin=dict(id='example.hello', execution='PROCESS', api=dict(minimum=1, maximum=1),
                                    integrity={'blob': library.hashlib.sha256(payload).hexdigest()}))
        with zipfile.ZipFile(self.package, 'w') as z:
            z.writestr('manifest.json', json.dumps(manifest))
            z.writestr('payload/blob', payload)
        with patch.object(library, 'LIMIT', 1): out = self.prepare()
        parts = json.loads((out / 'catalog.json').read_text(encoding='utf-8'))['plugins'][0]['releases'][0]['parts']
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0]['url'].endswith('.part001'))
        self.assertEqual(b''.join((out / Path(p['url']).name).read_bytes() for p in parts), self.package.read_bytes())

    def test_publish_refuses_tampered_asset_without_remote_call(self):
        out = self.prepare()
        next(out.glob('*.txplugin')).write_bytes(b'bad')
        with patch.object(library.subprocess, 'run') as remote:
            with self.assertRaises(ValueError): library.publish(out, github_only=True)
            remote.assert_not_called()

    def test_publish_updates_index_only_after_remote_success(self):
        out = self.prepare()
        with patch.object(library.subprocess, 'run', side_effect=RuntimeError('upload failed')):
            with self.assertRaises(RuntimeError): library.publish(out, github_only=True)
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))['revision'], 1)
        with patch.object(library.subprocess, 'run'): library.publish(out, github_only=True)
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))['revision'], 2)

    def test_private_key_cannot_be_created_inside_repo(self):
        with self.assertRaises(ValueError): library.generate_key(self.root / 'bad.pem')

    def test_dual_publish_records_only_uploaded_verified_mirror(self):
        out = self.prepare()
        url = 'https://gitee.com/wang_ke_bin/taixu-library/releases/download/hello-plugin-v1.0.0/hello-plugin-1.0.0.txplugin'
        with patch.object(library, 'publish_gitee', return_value={'hello-plugin-1.0.0.txplugin': url}), patch.object(library.subprocess, 'run'):
            library.publish(out, self.private)
        catalog = json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(catalog['plugins'][0]['releases'][0]['parts'][0]['mirrors']['GITEE'], url)
        public = library.serialization.load_der_public_key(base64.b64decode((self.root / 'public-key.txt').read_text(encoding='utf-8')))
        public.verify(base64.b64decode((self.root / 'catalog.json.sig').read_bytes()), (self.root / 'catalog.json').read_bytes())

    def test_failed_gitee_publish_does_not_publish_github_or_update_index(self):
        out = self.prepare()
        with patch.object(library, 'publish_gitee', side_effect=RuntimeError('quota exceeded')), patch.object(library.subprocess, 'run') as remote:
            with self.assertRaises(RuntimeError): library.publish(out, self.private)
            remote.assert_not_called()
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))['revision'], 1)

    def test_large_packages_are_rejected_before_either_host_is_called(self):
        out = self.prepare()
        with patch.object(library, 'SMALL_LIMIT', 1), patch.object(library, 'publish_gitee') as gitee, patch.object(library.subprocess, 'run') as github:
            with self.assertRaises(ValueError): library.publish(out, self.private, github_only=True)
            github.assert_not_called()
            gitee.assert_not_called()

    def test_gitee_only_release_never_advertises_unpublished_github_asset(self):
        out = self.prepare()
        url = 'https://gitee.com/wang_ke_bin/taixu-library/releases/download/hello-plugin-v1.0.0/hello-plugin-1.0.0.txplugin'
        with patch.object(library, 'publish_gitee', return_value={'hello-plugin-1.0.0.txplugin': url}):
            library.publish_gitee_only(out, self.private)
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))['plugins'], [])
        mirrored = json.loads((self.root / 'catalog-gitee.json').read_text(encoding='utf-8'))
        self.assertEqual(mirrored['plugins'][0]['releases'][0]['parts'][0]['url'], url)
        public = library.serialization.load_der_public_key(base64.b64decode((self.root / 'public-key.txt').read_text(encoding='utf-8')))
        public.verify(base64.b64decode((self.root / 'catalog-gitee.json.sig').read_bytes()), (self.root / 'catalog-gitee.json').read_bytes())

    def test_legacy_package_uses_stable_legacy_identity(self):
        with zipfile.ZipFile(self.package, 'w') as z:
            z.writestr('manifest.json', json.dumps(dict(id='hello-plugin', version='1.0.0', name='Hello', description='Legacy')))
            z.writestr('payload/file', b'legacy data')
        out = self.prepare()
        plugin = json.loads((out / 'catalog.json').read_text(encoding='utf-8'))['plugins'][0]
        self.assertEqual(plugin['pluginId'], 'legacy.tool-hello-plugin')
        self.assertEqual(plugin['releases'][0]['packageSchema'], 1)


if __name__ == '__main__': unittest.main()
