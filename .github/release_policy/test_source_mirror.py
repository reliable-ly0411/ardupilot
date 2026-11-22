"""Source mirror regression coverage. AP_FLAKE8_CLEAN"""
import importlib.util
import json
import sys
import unittest

from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('source_mirror', Path(__file__).with_name('source_mirror.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SourceMirrorTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.entry = {'tag': 'Copter-9.0.0', 'upstream_sha': self.sha, 'published_at': '2026-11-01T00:00:00Z'}
        self.release = dict(self.entry, id=123, tag_name=self.entry['tag'], draft=False, prerelease=False,
                            body='Original upstream notes', name='Copter 9')

    def test_notes_preserved_and_source_only_disclosed(self):
        body = m.release_body(self.release, self.sha)
        self.assertTrue(body.endswith('Original upstream notes'))
        self.assertIn('No compiled firmware', body)
        self.assertIn(self.sha, body)
        self.assertIn('Submodules are references', body)

    def test_revoked_renamed_identity_blocks(self):
        for changed in ({'id': 124}, {'draft': True}, {'prerelease': True}, {'tag_name': 'other'},
                        {'published_at': '2020-01-01T00:00:00Z'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                m.verify_release(dict(self.release, **changed), self.entry, '123')

    def test_changed_notes_are_blocked(self):
        entry = dict(self.entry, notes_sha256=m.notes_digest(self.release))
        with self.assertRaisesRegex(ValueError, 'notes changed'):
            m.verify_release(dict(self.release, body='changed'), entry, '123')

    def test_annotated_tag_checks_peeled_commit(self):
        refs = 'b' * 40 + ' refs/tags/Copter-9.0.0\n' + self.sha + ' refs/tags/Copter-9.0.0^{}'
        _, calls, _ = self.run_mirror(refs=refs, tag_object='b' * 40)
        self.assertFalse(any(c.args[0] == 'push' for c in calls))

    def test_selector_preserves_first_recognition_date_on_retry(self):
        entry = dict(self.entry, status='failed', attempts=1)
        state = {'releases': {'123': entry}}
        config = {'families': {'Copter': r'^Copter-\d+\.\d+\.\d+$'},
                  'bootstrap_at': '2026-10-20T13:55:00Z', 'max_attempts': 3}
        changed = dict(self.release, published_at='2026-11-02T00:00:00Z')
        with patch.object(m, 'mirror') as mirror:
            selected = m.policy.select_release([changed], state, config, lambda _: self.sha)
            if selected:
                mirror(config, *selected)
            mirror.assert_not_called()
        self.assertIsNone(selected)
        self.assertEqual(entry['status'], 'blocked')
        self.assertEqual(entry['published_at'], self.entry['published_at'])

    def run_mirror(self, refs='', existing=None, sha=None, license_text='GNU GENERAL PUBLIC LICENSE',
                   tag_object=None, changed_tag_object=None, verification_refs=None):
        tag_object = tag_object or self.sha
        object_reads = 0

        def git(*args):
            nonlocal object_reads
            if args[0] == 'show':
                return license_text
            if args[0] == 'rev-parse':
                object_reads += 1
                return changed_tag_object if changed_tag_object and object_reads > 1 else tag_object
            if args[0] == 'ls-remote':
                if '--refs' in args:
                    return verification_refs if verification_refs is not None else tag_object + ' refs/tags/Copter-9.0.0'
                return refs
            return ''
        with patch.object(m.policy, 'git', side_effect=git) as g, \
                patch.object(m.policy, 'resolve_tag', return_value=sha or self.sha), \
                patch.object(m.policy, 'command', return_value=json.dumps([existing or []])), \
                patch.object(m, 'api', side_effect=lambda path, payload=None:
                             {'html_url': 'https://example/release'} if payload else self.release) as a:
            result = m.mirror({'upstream': m.UPSTREAM}, '123', self.entry)
            return result, g.call_args_list, a.call_args_list

    def test_missing_tag_created_with_absent_lease_and_source_release_only(self):
        result, calls, api_calls = self.run_mirror()
        self.assertEqual(result, 'https://example/release')
        self.assertIn(('push', '--atomic', '--force-with-lease=refs/tags/Copter-9.0.0:',
                       'origin', self.sha + ':refs/tags/Copter-9.0.0'), [c.args for c in calls])
        payload = api_calls[-1].args[1]
        self.assertFalse(payload['draft'])
        self.assertFalse(payload['prerelease'])
        self.assertNotIn('assets', payload)

    def test_new_annotated_tag_after_sync_keeps_direct_object(self):
        _, calls, _ = self.run_mirror(tag_object='b' * 40)
        pushes = [c.args for c in calls if c.args[0] == 'push']
        self.assertEqual(pushes, [('push', '--atomic', '--force-with-lease=refs/tags/Copter-9.0.0:',
                                  'origin', 'b' * 40 + ':refs/tags/Copter-9.0.0')])

    def test_same_commit_different_annotated_object_is_never_overwritten(self):
        refs = 'c' * 40 + ' refs/tags/Copter-9.0.0\n' + self.sha + ' refs/tags/Copter-9.0.0^{}'
        with self.assertRaisesRegex(ValueError, 'never overwrite'):
            self.run_mirror(refs=refs, tag_object='b' * 40)

    def test_upstream_annotation_change_before_publication_blocks(self):
        with self.assertRaisesRegex(ValueError, 'changed before publication'):
            self.run_mirror(tag_object='b' * 40, changed_tag_object='c' * 40)

    def test_downstream_tag_change_before_publication_blocks(self):
        with self.assertRaisesRegex(ValueError, 'Downstream tag changed'):
            self.run_mirror(verification_refs='c' * 40 + ' refs/tags/Copter-9.0.0')

    def test_moved_upstream_blocks(self):
        with self.assertRaisesRegex(ValueError, 'Upstream tag moved'):
            self.run_mirror(sha='b' * 40)

    def test_conflicting_local_tag_blocks(self):
        with self.assertRaisesRegex(ValueError, 'never overwrite'):
            self.run_mirror(refs='b' * 40 + ' refs/tags/Copter-9.0.0')

    def test_missing_license_blocks(self):
        with self.assertRaisesRegex(ValueError, 'license'):
            self.run_mirror(license_text='')

    def test_identical_release_is_idempotent(self):
        release = dict(self.release, body=m.release_body(self.release, self.sha), html_url='same')
        result, calls, api_calls = self.run_mirror(refs=self.sha + ' refs/tags/Copter-9.0.0', existing=[release])
        self.assertEqual(result, 'same')
        self.assertFalse(any(c.args[0] == 'push' for c in calls))
        self.assertTrue(all(len(c.args) == 1 for c in api_calls))

    def test_different_existing_release_is_never_rewritten(self):
        with self.assertRaisesRegex(ValueError, 'never overwrite'):
            self.run_mirror(refs=self.sha + ' refs/tags/Copter-9.0.0', existing=[self.release])


if __name__ == '__main__':
    unittest.main()
