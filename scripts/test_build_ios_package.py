import base64
import os
import tempfile
import unittest
from pathlib import Path

from build_ios import package_unsigned_ipa, stage_payload
from configure_ios_signing import decode, export_options, required_secrets


class PayloadLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='duanju-ios-ipa-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.application = self.root / 'Runner.app'
        (self.application / 'Frameworks').mkdir(parents=True)
        (self.application / 'Runner').write_bytes(b'\xcf\xfa\xed\xfe')
        (self.application / 'Info.plist').write_text('<plist/>', encoding='utf-8')
        (self.application / 'Frameworks' / 'Flutter.framework').write_bytes(b'framework')

    def test_stage_payload_uses_ipa_directory_layout(self):
        payload = stage_payload(self.application, self.root / 'staging')
        self.assertEqual(payload.name, 'Payload')
        self.assertEqual(payload.parent, self.root / 'staging')
        self.assertTrue((payload / 'Runner.app' / 'Runner').is_file())
        self.assertTrue((payload / 'Runner.app' / 'Frameworks' / 'Flutter.framework').is_file())

    def test_stage_payload_replaces_stale_staging(self):
        stale = self.root / 'staging' / 'Payload' / 'Runner.app' / 'stale'
        stale.parent.mkdir(parents=True)
        stale.write_text('stale', encoding='utf-8')
        payload = stage_payload(self.application, self.root / 'staging')
        self.assertFalse((payload / 'Runner.app' / 'stale').exists())

    def test_stage_payload_preserves_symlinks(self):
        probe = self.root / 'probe-link'
        try:
            probe.symlink_to(self.application / 'Runner')
        except (OSError, NotImplementedError):
            self.skipTest('当前平台不支持创建符号链接')
        if not probe.is_symlink():
            self.skipTest('当前文件系统不支持符号链接')
        probe.unlink()
        (self.application / 'Frameworks' / 'Current').symlink_to('Flutter.framework')
        payload = stage_payload(self.application, self.root / 'staging')
        self.assertTrue((payload / 'Runner.app' / 'Frameworks' / 'Current').is_symlink())


class UnsignedIpaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='duanju-ios-ipa-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.application = self.root / 'Runner.app'
        self.application.mkdir(parents=True)
        (self.application / 'Runner').write_bytes(b'\xcf\xfa\xed\xfe')
        self.destination = self.root / 'dist' / 'hongguojian-0.2.55+61-ios-unsigned.ipa'
        self.destination.parent.mkdir(parents=True)

    @property
    def staging(self):
        return self.destination.parent / (self.destination.stem + '.payload')

    def test_package_unsigned_ipa_archives_payload(self):
        calls = []
        observed = {}

        def archiver(arguments):
            calls.append(arguments)
            payload = Path(arguments[-2])
            observed['payload'] = payload.name
            observed['binary'] = (payload / 'Runner.app' / 'Runner').is_file()
            observed['staged'] = payload.parent.is_dir()
            self.destination.write_bytes(b'PK\x03\x04')

        self.assertEqual(package_unsigned_ipa(self.application, self.destination, archiver=archiver),
                         self.destination)
        self.assertEqual(len(calls), 1)
        arguments = calls[0]
        self.assertEqual(arguments[0], 'ditto')
        self.assertIn('--keepParent', arguments)
        self.assertEqual(arguments[-1], str(self.destination))
        self.assertEqual(observed['payload'], 'Payload')
        self.assertTrue(observed['binary'])
        self.assertTrue(observed['staged'])
        self.assertFalse(self.staging.exists())

    def test_package_unsigned_ipa_requires_output(self):
        with self.assertRaises(SystemExit):
            package_unsigned_ipa(self.application, self.destination, archiver=lambda arguments: None)
        self.assertFalse(self.staging.exists())

    def test_package_unsigned_ipa_cleans_staging_on_failure(self):
        def archiver(arguments):
            raise RuntimeError('ditto 失败')

        with self.assertRaises(RuntimeError):
            package_unsigned_ipa(self.application, self.destination, archiver=archiver)
        self.assertFalse(self.staging.exists())


class SigningSecretTests(unittest.TestCase):
    def test_decode_rejects_invalid_base64(self):
        os.environ['DUANJU_TEST_SECRET'] = '不是 Base64'
        self.addCleanup(os.environ.pop, 'DUANJU_TEST_SECRET', None)
        with self.assertRaises(SystemExit):
            decode('DUANJU_TEST_SECRET')

    def test_decode_rejects_empty_value(self):
        os.environ['DUANJU_TEST_SECRET'] = '   '
        self.addCleanup(os.environ.pop, 'DUANJU_TEST_SECRET', None)
        with self.assertRaises(SystemExit):
            decode('DUANJU_TEST_SECRET')

    def test_decode_accepts_wrapped_base64(self):
        os.environ['DUANJU_TEST_SECRET'] = base64.b64encode(b'certificate').decode('ascii')
        self.addCleanup(os.environ.pop, 'DUANJU_TEST_SECRET', None)
        self.assertEqual(decode('DUANJU_TEST_SECRET'), b'certificate')

    def test_export_options_stays_outside_repository_source(self):
        self.assertEqual(export_options.name, 'ExportOptions.plist')
        self.assertEqual(export_options.parent.name, 'ios')
        self.assertIn('IOS_CERTIFICATE_BASE64', required_secrets)
        self.assertIn('IOS_PROVISIONING_PROFILE_BASE64', required_secrets)


if __name__ == '__main__':
    unittest.main()
