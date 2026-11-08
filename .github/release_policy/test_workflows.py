"""Fail closed if a heavy workflow regains an automatic entry point. AP_FLAKE8_CLEAN"""
import json
import re
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads(Path(__file__).with_name('config.json').read_text())


class WorkflowPolicyTests(unittest.TestCase):
    def test_heavy_workflows_only_manual_or_reusable(self):
        for name in CONFIG['heavy_workflows']:
            with self.subTest(workflow=name):
                text = (ROOT / 'workflows' / name).read_text()
                block = re.search(r"^'?on'?\s*:[\s\S]*?(?=^[A-Za-z][A-Za-z_-]*:)", text, re.M).group()
                self.assertIn('workflow_dispatch:', block)
                for trigger in ('push:', 'pull_request:', 'schedule:', 'workflow_run:', 'release:', 'repository_dispatch:'):
                    self.assertNotIn(trigger, block)

    def test_automatic_recipes_reconstruct_pinned_source(self):
        for name in CONFIG['automatic_workflows']:
            text = (ROOT / 'workflows' / name).read_text()
            self.assertIn('workflow_call:', text)
            self.assertIn('RELEASE_MANIFEST: ${{ inputs.release_manifest }}', text)
            self.assertIn('python3 .github/release_policy/policy.py materialize', text)
            self.assertIn('ref: ${{ inputs.source_ref || github.sha }}', text)

    def test_build_is_conditional_and_manual_sync_defaults_to_sync_only(self):
        text = (ROOT / 'workflows/sync-upstream.yml').read_text()
        self.assertIn("python3 .github/release_policy/source_mirror.py", text)
        self.assertNotIn("uses: ./.github/workflows/release-build.yml", text)
        self.assertIn('default: sync', text)
        self.assertIn("github.event_name == 'schedule' || inputs.action == 'check-releases'", text)
        self.assertNotIn('workflow_run:', text)
        self.assertIn('cancel-in-progress: false', text)

    def test_full_sync_is_guarded_and_master_has_no_early_push(self):
        text = (ROOT / 'workflows/sync-upstream.yml').read_text()
        self.assertIn("github.repository == 'reliable-ly0411/ardupilot' && github.ref == 'refs/heads/master'", text)
        self.assertIn('cron: "43 4 * * *"', text)
        self.assertIn('group: release-policy-master', text)
        self.assertIn('timeout-minutes: 20', text)
        self.assertIn('id: merge', text)
        self.assertIn('master_before=$(git rev-parse HEAD)', text)
        self.assertIn('EXPECTED_MASTER: ${{ steps.merge.outputs.master_before }}', text)
        self.assertIn('sync_refs.py --expected-master "$EXPECTED_MASTER" --apply', text)
        self.assertNotIn('git push origin', text)
        self.assertNotIn('git push --mirror', text)
        self.assertNotIn('git push --tags', text)
        self.assertNotIn('git push --force', text)

    def test_sync_tests_precede_master_preparation_and_atomic_push(self):
        text = (ROOT / 'workflows/sync-upstream.yml').read_text()
        tests = text.index('python3 -m unittest discover')
        merge = text.index('id: merge')
        atomic_push = text.index('sync_refs.py --expected-master')
        source_release = text.index('python3 .github/release_policy/source_mirror.py')
        self.assertLess(tests, merge)
        self.assertLess(merge, atomic_push)
        self.assertLess(atomic_push, source_release)

    def test_publication_interface_does_not_rebuild_or_publish(self):
        text = (ROOT / 'workflows/promote-release.yml').read_text()
        self.assertIn('evidence.py verify', text)
        self.assertNotIn('materialize', text)
        self.assertNotIn('workflow_call:', text)
        self.assertIn('exit 1', text)

    def test_murmur_fixture_firmware_is_not_uploaded(self):
        path = ROOT / 'workflows/murmur.yml'
        if path.exists():
            text = path.read_text()
            self.assertIn('ci-only-not-a-secret', text)
            self.assertNotIn('upload-artifact', text)
            self.assertNotIn('gh release', text)

    def test_inherited_owner_guards_remain(self):
        for filename in ('release-exe.yml', 'release-pypi.yml'):
            path = ROOT / 'workflows' / filename
            if path.exists():
                self.assertIn("github.repository == 'jnz/INSLIB'", path.read_text())


if __name__ == '__main__':
    unittest.main()
