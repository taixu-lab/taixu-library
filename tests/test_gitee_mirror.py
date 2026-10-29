from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gitee_mirror as mirror


class MirrorTest(unittest.TestCase):
    def test_large_package_never_touches_gitee(self):
        client = Mock()
        self.assertEqual(mirror.publish_assets(Path('.'), 'tag', [dict(size=mirror.SMALL_LIMIT+1)], client=client), {})
        client.release.assert_not_called()

    def test_new_and_existing_attachments_must_match_hash(self):
        client = Mock()
        client.release.return_value = {'id': 12}
        client.request.return_value = []
        url = 'https://gitee.com/a/b/releases/download/v1/file.txplugin'
        client.upload.return_value = dict(browser_download_url=url)
        part = dict(url='https://github.com/a/b/releases/download/v1/file.txplugin', size=3, sha256='abc')
        with patch.object(mirror, 'remote_hash', return_value=(3, 'abc')):
            self.assertEqual(mirror.publish_assets(Path('.'), 'v1', [part], client=client), {'file.txplugin': url})
        client.request.return_value = [dict(name='file.txplugin', browser_download_url=url)]
        client.upload.reset_mock()
        with patch.object(mirror, 'remote_hash', return_value=(3, 'bad')):
            with self.assertRaises(ValueError): mirror.publish_assets(Path('.'), 'v1', [part], client=client)
        client.upload.assert_not_called()

    def test_auth_bearing_api_response_cannot_supply_arbitrary_urls(self):
        for url in ['http://gitee.com/file', 'https://evil.com/file', 'https://gitee.com/file?access_token=secret']:
            with self.assertRaises(ValueError): mirror.download_url(dict(browser_download_url=url))


if __name__ == '__main__': unittest.main()
