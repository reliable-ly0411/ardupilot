"""Real-Git, network-free ref sync regression tests. AP_FLAKE8_CLEAN"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sync_refs', Path(__file__).with_name('sync_refs.py'))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class RefSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.upstream = self.root / 'upstream.git'
        self.fork = self.root / 'fork.git'
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='Sync Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Sync Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for path in (self.work, self.upstream, self.fork):
            self.command('init', '--initial-branch=master', *([] if path == self.work else ['--bare']), str(path))
        self.command('remote', 'add', 'origin', str(self.fork))
        self.base = self.commit('base')
        self.command('update-ref', m.MASTER, self.base)
        for remote in (self.upstream, self.fork):
            self.publish(remote, m.MASTER, self.base)
        self.git = m.Git(self.work)
        self.sync = m.SyncRefs(self.git, str(self.upstream), str(self.fork))

    def command(self, *args, input=None, cwd=None, check=True):
        result = subprocess.run(['git', '-C', str(cwd or (self.work if self.work.exists() else self.root)), *args],
                                input=input, text=True, capture_output=True, env=self.env, check=False)
        if check and result.returncode:
            self.fail(f'Fixture Git command {args[0]} failed: {result.stderr}')
        return result.stdout.strip() if check else result

    def commit(self, message, *parents):
        tree = self.command('mktree', input='')
        args = ['commit-tree', tree]
        for parent in parents:
            args += ['-p', parent]
        return self.command(*args, input=message + '\n')

    def tag_object(self, name, target, target_type='commit', signed=False):
        body = (f'object {target}\ntype {target_type}\ntag {name}\n'
                'tagger Sync Test <test@example.invalid> 1700000000 +0000\n\nOriginal tag message\n')
        if signed:
            # Signature bytes are opaque; synchronization must not strip or regenerate them.
            body += '-----BEGIN PGP SIGNATURE-----\nopaque-test-signature\n-----END PGP SIGNATURE-----\n'
        return self.command('hash-object', '-w', '-t', 'tag', '--stdin', input=body)

    def publish(self, remote, ref, oid):
        self.command('push', '--quiet', str(remote), '+' + oid + ':' + ref)

    def snapshot(self):
        return self.git.snapshot(str(self.fork))

    def plan(self):
        return self.sync.plan(self.base)

    def apply(self):
        plan = self.plan()
        self.assertEqual(plan.conflicts, [])
        self.sync.apply(plan)
        return plan

    def test_new_and_fast_forward_branches(self):
        tip = self.commit('advance', self.base)
        self.publish(self.upstream, 'refs/heads/new', tip)
        self.publish(self.upstream, 'refs/heads/existing', tip)
        self.publish(self.fork, 'refs/heads/existing', self.base)
        plan = self.apply()
        self.assertEqual(plan.counts['new branches'], 1)
        self.assertEqual(plan.counts['fast-forward branches'], 1)
        self.assertEqual(self.snapshot()['refs/heads/existing'], tip)
        self.assertEqual(self.snapshot()['refs/heads/new'], tip)

    def test_fork_ahead_and_fork_only_preserved_without_deletions(self):
        ahead = self.commit('downstream', self.base)
        self.publish(self.upstream, 'refs/heads/shared', self.base)
        self.publish(self.fork, 'refs/heads/shared', ahead)
        for name in ('copilot/fix', 'release-policy-state', 'custom'):
            self.publish(self.fork, 'refs/heads/' + name, ahead)
        for name in ('private', 'sync-backup/2026-11-08/old'):
            self.publish(self.fork, 'refs/tags/' + name, ahead)
        before = self.snapshot()
        plan = self.apply()
        self.assertEqual(plan.fork_ahead, ['refs/heads/shared'])
        self.assertEqual(len(plan.fork_only), 3)
        self.assertEqual(plan.counts['preserved fork-only tags'], 2)
        self.assertEqual(self.snapshot(), before)

    def test_divergent_branch_blocks_other_valid_changes(self):
        self.publish(self.upstream, 'refs/heads/shared', self.commit('upstream', self.base))
        self.publish(self.fork, 'refs/heads/shared', self.commit('fork', self.base))
        self.publish(self.upstream, 'refs/tags/new', self.base)
        before = self.snapshot()
        plan = self.plan()
        self.assertIn('Divergent branch: refs/heads/shared', plan.conflicts)
        with self.assertRaisesRegex(m.SyncError, 'conflicts'):
            self.sync.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_prepared_master_merge_is_in_same_transaction(self):
        fork_tip = self.commit('fork customization', self.base)
        upstream_tip = self.commit('upstream advance', self.base)
        self.publish(self.fork, m.MASTER, fork_tip)
        self.publish(self.upstream, m.MASTER, upstream_tip)
        merged = self.commit('prepared merge', fork_tip, upstream_tip)
        self.command('update-ref', m.MASTER, merged)
        self.publish(self.upstream, 'refs/tags/new', upstream_tip)
        plan = self.sync.plan(fork_tip)
        self.assertEqual(plan.conflicts, [])
        with patch.object(self.git, 'run', wraps=self.git.run) as run:
            self.sync.apply(plan)
        pushes = [call.args for call in run.call_args_list if call.args[0] == 'push']
        self.assertEqual(len(pushes), 1)
        self.assertIn('--atomic', pushes[0])
        self.assertEqual({arg for arg in pushes[0] if arg.startswith('--force-with-lease=')},
                         {f'--force-with-lease={u.ref}:{u.old}' for u in plan.updates.values()})
        self.assertFalse(any(arg in pushes[0] for arg in ('--mirror', '--tags', '--force', '--prune', '--delete')))
        self.assertIn(f'--force-with-lease={m.MASTER}:{fork_tip}', pushes[0])
        self.assertIn(f'{merged}:{m.MASTER}', pushes[0])
        self.assertIn(upstream_tip + ':refs/tags/new', pushes[0])
        self.assertEqual(self.snapshot()[m.MASTER], merged)

    def test_master_requires_both_ancestries(self):
        fork_tip = self.commit('fork advance', self.base)
        upstream_tip = self.commit('upstream advance', self.base)
        self.publish(self.fork, m.MASTER, fork_tip)
        self.publish(self.upstream, m.MASTER, upstream_tip)
        for invalid in (self.base, fork_tip, upstream_tip):
            with self.subTest(head=invalid):
                self.command('update-ref', m.MASTER, invalid)
                plan = self.sync.plan(fork_tip)
                self.assertTrue(any('both expected' in conflict for conflict in plan.conflicts))
                with self.assertRaises(m.SyncError):
                    self.sync.apply(plan)

    def test_stale_expected_master_blocks_before_fetch(self):
        self.publish(self.fork, m.MASTER, self.commit('race', self.base))
        with patch.object(self.git, 'fetch_snapshot', wraps=self.git.fetch_snapshot) as fetch:
            plan = self.plan()
        fetch.assert_not_called()
        self.assertTrue(any('pre-merge master' in c for c in plan.conflicts))

    def test_upstream_master_changed_after_preparation_blocks(self):
        self.publish(self.upstream, m.MASTER, self.commit('new upstream', self.base))
        plan = self.plan()
        self.assertTrue(any('both expected' in c for c in plan.conflicts))

    def test_new_lightweight_annotated_nested_and_signed_objects_exact(self):
        annotated = self.tag_object('annotated', self.base)
        nested = self.tag_object('nested', annotated, 'tag')
        signed = self.tag_object('signed', nested, 'tag', signed=True)
        tags = {'lightweight': self.base, 'annotated': annotated, 'nested': nested, 'signed': signed}
        for name, oid in tags.items():
            self.publish(self.upstream, 'refs/tags/' + name, oid)
        self.apply()
        refs = self.snapshot()
        for name, oid in tags.items():
            with self.subTest(tag=name):
                self.assertEqual(refs['refs/tags/' + name], oid)
                self.assertEqual(self.command('cat-file', '-p', oid, cwd=self.fork), self.command('cat-file', '-p', oid))
        self.assertNotEqual(signed, self.base)

    def test_fixed_tag_conflict_even_when_peeled_commit_matches(self):
        self.publish(self.upstream, 'refs/tags/fixed', self.tag_object('fixed', self.base))
        self.publish(self.fork, 'refs/tags/fixed', self.base)
        plan = self.plan()
        self.assertIn('Fixed tag direct-object conflict: refs/tags/fixed', plan.conflicts)
        with self.assertRaises(m.SyncError):
            self.sync.apply(plan)
        self.assertEqual(self.snapshot()['refs/tags/fixed'], self.base)

    def rolling_setup(self):
        old = self.tag_object('old', self.base, signed=True)
        new = self.tag_object('new', self.base)
        tag = 'refs/tags/ArduCopter-stable'
        self.publish(self.fork, tag, old)
        self.publish(self.upstream, tag, new)
        return old, new, tag, 'refs/tags/sync-backup/rolling/ArduCopter-stable/' + old

    def test_rolling_backup_preserves_direct_object_and_repeat_is_noop(self):
        old, new, tag, prefix = self.rolling_setup()
        plan = self.apply()
        backups = [u for u in plan.updates.values() if u.reason == 'new backup']
        self.assertEqual(len(backups), 1)
        backup = backups[0].ref
        self.assertTrue(backup.startswith(prefix + '-'))
        self.assertEqual(len(backup[len(prefix) + 1:]), 32)
        self.assertEqual(plan.updates[backup].old, '')
        self.assertEqual(plan.updates[backup].new, old)
        self.assertEqual(self.snapshot()[tag], new)
        self.assertEqual(self.snapshot()[backup], old)
        self.assertIn(backup, plan.summary())
        before = self.snapshot()
        with self.assertRaisesRegex(m.SyncError, 'Fork refs changed'):
            self.sync.apply(plan)
        again = self.apply()
        self.assertEqual(again.updates, {})
        self.assertEqual(self.snapshot(), before)

    def test_existing_matching_backup_retained_but_never_reused(self):
        old, new, tag, legacy = self.rolling_setup()
        self.publish(self.fork, legacy, old)
        plan = self.apply()
        self.assertNotIn(legacy, plan.updates)
        backups = [u for u in plan.updates.values() if u.reason == 'new backup']
        self.assertEqual(len(backups), 1)
        self.assertNotEqual(backups[0].ref, legacy)
        self.assertEqual(backups[0].old, '')
        self.assertEqual(self.snapshot()[tag], new)
        self.assertEqual(self.snapshot()[legacy], old)
        self.assertEqual(self.snapshot()[backups[0].ref], old)

    def test_fresh_backup_uuid_collision_blocks_all_writes_even_same_oid(self):
        old, new, tag, prefix = self.rolling_setup()
        transaction = m.uuid.UUID('1' * 32)
        backup = prefix + '-' + transaction.hex
        for existing in (self.base, old):
            with self.subTest(existing=existing):
                self.publish(self.fork, backup, existing)
                before = self.snapshot()
                with patch.object(m.uuid, 'uuid4', return_value=transaction) as make_uuid:
                    plan = self.plan()
                self.assertTrue(any('Fresh backup ref already exists' in c for c in plan.conflicts))
                self.assertEqual(make_uuid.call_count, 1)
                with self.assertRaises(m.SyncError):
                    self.sync.apply(plan)
                self.assertEqual(self.snapshot(), before)

    def receive_race(self, ref, oid, commands):
        hook = self.fork / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\n'
                        'cat > "' + str(commands) + '"\n'
                        'unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n'
                        'git update-ref ' + ref + ' ' + oid + '\n')
        hook.chmod(0o755)
        real_run = self.git.run

        def with_server_hook(*args, **kwargs):
            if args[0] == 'push':
                return real_run('-c', 'core.hooksPath=' + str(self.fork / 'hooks'), *args, **kwargs)
            return real_run(*args, **kwargs)

        return patch.object(self.git, 'run', side_effect=with_server_hook)

    def test_backup_creation_race_after_advertisement_prevents_alias_update(self):
        old, new, tag, prefix = self.rolling_setup()
        self.publish(self.upstream, 'refs/tags/other-new', self.base)
        plan = self.plan()
        backup = next(u.ref for u in plan.updates.values() if u.reason == 'new backup')
        commands = self.root / 'receive-commands'
        with self.receive_race(backup, self.base, commands) as run:
            with self.assertRaisesRegex(m.SyncError, 'no fallback or automatic retry'):
                self.sync.apply(plan)
        pushes = [call.args for call in run.call_args_list if call.args[0] == 'push']
        self.assertEqual(len(pushes), 1)
        self.assertIn('--force-with-lease=' + backup + ':', pushes[0])
        self.assertIn('0' * len(old) + ' ' + old + ' ' + backup, commands.read_text().splitlines())
        refs = self.snapshot()
        self.assertEqual(refs[backup], self.base)  # Only the independent fixture race wrote this ref.
        self.assertEqual(refs[tag], old)
        self.assertNotIn('refs/tags/other-new', refs)

    def test_legacy_backup_mutation_during_receive_cannot_remove_fresh_protection(self):
        old, new, tag, legacy = self.rolling_setup()
        self.publish(self.fork, legacy, old)
        plan = self.plan()
        backup = next(u.ref for u in plan.updates.values() if u.reason == 'new backup')
        commands = self.root / 'receive-commands'
        with self.receive_race(legacy, self.base, commands):
            self.sync.apply(plan)
        self.assertIn('0' * len(old) + ' ' + old + ' ' + backup, commands.read_text().splitlines())
        self.assertFalse(any(line.endswith(' ' + legacy) for line in commands.read_text().splitlines()))
        refs = self.snapshot()
        self.assertEqual(refs[tag], new)
        self.assertEqual(refs[backup], old)
        self.assertEqual(refs[legacy], self.base)  # Changed by the independent fixture race, never by the sync.

    def test_backup_prefix_collision_blocks_before_push(self):
        self.rolling_setup()
        for prefix in ('sync-backup', 'sync-backup/rolling'):
            with self.subTest(prefix=prefix):
                self.command('update-ref', 'refs/tags/' + prefix, self.base, cwd=self.fork)
                plan = self.plan()
                self.assertTrue(any('Ref namespace collision' in c for c in plan.conflicts))
                with self.assertRaises(m.SyncError):
                    self.sync.apply(plan)
                self.command('update-ref', '-d', 'refs/tags/' + prefix, cwd=self.fork)

    def test_branch_prefix_collision_with_fork_only_branch(self):
        self.publish(self.fork, 'refs/heads/custom', self.base)
        self.publish(self.upstream, 'refs/heads/custom/nested', self.base)
        self.assertTrue(any('namespace collision' in c for c in self.plan().conflicts))

    def test_all_and_only_fourteen_rolling_aliases_allowed(self):
        self.assertEqual(len(m.ROLLING_TAGS), 14)
        tip = self.commit('tip', self.base)
        for tag in m.ROLLING_TAGS:
            self.publish(self.fork, 'refs/tags/' + tag, self.base)
            self.publish(self.upstream, 'refs/tags/' + tag, tip)
        plan = self.apply()
        self.assertEqual(plan.counts['rolling tags'], 14)
        self.assertEqual(plan.counts['new backup'], 14)
        self.publish(self.fork, 'refs/tags/ArduCopter-beta-unknown', self.base)
        self.publish(self.upstream, 'refs/tags/ArduCopter-beta-unknown', tip)
        self.assertTrue(any('Fixed tag' in c for c in self.plan().conflicts))

    def test_plan_does_not_touch_local_branches_tags_or_fetch_head(self):
        self.publish(self.upstream, 'refs/heads/new', self.base)
        self.publish(self.upstream, 'refs/tags/new', self.base)
        self.command('update-ref', 'refs/tags/local-only', self.base)
        fetch_head = self.work / '.git' / 'FETCH_HEAD'
        fetch_head.write_text('sentinel\n')
        before = self.command('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags')
        remote_before = self.snapshot()
        self.plan()
        self.assertEqual(self.snapshot(), remote_before)
        self.assertEqual(fetch_head.read_text(), 'sentinel\n')
        self.assertEqual(self.command('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags'), before)

    def test_upstream_or_fork_change_before_push_stops(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        for remote, label in ((self.upstream, 'Upstream'), (self.fork, 'Fork')):
            with self.subTest(remote=label):
                plan = self.plan()
                self.publish(remote, 'refs/tags/race-' + label, self.base)
                with patch.object(self.git, 'run', wraps=self.git.run) as run:
                    with self.assertRaisesRegex(m.SyncError, label + ' refs changed before push'):
                        self.sync.apply(plan)
                self.assertFalse(any(call.args[0] == 'push' for call in run.call_args_list))

    def test_local_prepared_head_change_stops(self):
        plan = self.plan()
        self.command('update-ref', m.MASTER, self.commit('changed', self.base))
        with self.assertRaisesRegex(m.SyncError, 'local master changed'):
            self.sync.apply(plan)

    def test_advertisement_fetch_race_stops(self):
        self.publish(self.upstream, 'refs/tags/moving', self.base)
        real_fetch = self.git.fetch_snapshot
        changed = False

        def race(endpoint, refs, namespace):
            nonlocal changed
            if not changed:
                changed = True
                self.publish(self.upstream, 'refs/tags/moving', self.commit('race', self.base))
            return real_fetch(endpoint, refs, namespace)

        before = self.snapshot()
        with patch.object(self.git, 'fetch_snapshot', side_effect=race):
            with self.assertRaisesRegex(m.SyncError, 'changed during fetch'):
                self.plan()
        self.assertEqual(self.snapshot(), before)

    def test_lease_race_rejects_entire_atomic_transaction(self):
        tip = self.commit('tip', self.base)
        racing = self.commit('concurrent', self.base)
        self.publish(self.upstream, 'refs/heads/shared', tip)
        self.publish(self.fork, 'refs/heads/shared', self.base)
        self.publish(self.upstream, 'refs/tags/other-new', tip)
        plan = self.plan()
        real_run = self.git.run
        pushes = []

        def race(*args, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                self.publish(self.fork, 'refs/heads/shared', racing)
            return real_run(*args, **kwargs)

        with patch.object(self.git, 'run', side_effect=race):
            with self.assertRaisesRegex(m.SyncError, 'no fallback or automatic retry'):
                self.sync.apply(plan)
        refs = self.snapshot()
        self.assertEqual(len(pushes), 1)
        self.assertEqual(refs['refs/heads/shared'], racing)
        self.assertNotIn('refs/tags/other-new', refs)

    def test_new_ref_lease_race_does_not_overwrite(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        plan = self.plan()
        racing = self.commit('race')
        real_run = self.git.run

        def race(*args, **kwargs):
            if args[0] == 'push':
                self.publish(self.fork, 'refs/tags/new', racing)
            return real_run(*args, **kwargs)

        with patch.object(self.git, 'run', side_effect=race):
            with self.assertRaises(m.SyncError):
                self.sync.apply(plan)
        self.assertEqual(self.snapshot()['refs/tags/new'], racing)

    def test_server_rejection_leaves_master_tag_and_backup_unchanged(self):
        self.rolling_setup()
        tip = self.commit('master advance', self.base)
        self.publish(self.upstream, m.MASTER, tip)
        self.command('update-ref', m.MASTER, tip)
        hook = self.fork / 'hooks' / 'update'
        hook.write_text('#!/bin/sh\n[ "$1" != "refs/heads/master" ]\n')
        hook.chmod(0o755)
        # Local transport inherits client -c settings. Re-enable only this fixture server's hooks.
        self.command('config', 'core.hooksPath', str(self.fork / 'hooks'), cwd=self.fork)
        plan = self.plan()
        before = self.snapshot()
        real_run = self.git.run

        def with_server_hook(*args, **kwargs):
            if args[0] == 'push':
                return real_run('-c', 'core.hooksPath=' + str(self.fork / 'hooks'), *args, **kwargs)
            return real_run(*args, **kwargs)

        with patch.object(self.git, 'run', side_effect=with_server_hook):
            with self.assertRaisesRegex(m.SyncError, 'expected old values'):
                self.sync.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_workflow_permission_rejection_is_explained_without_retry(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        plan = self.plan()
        before = self.snapshot()
        real_run = self.git.run
        pushes = []

        def refuse_workflow(*args, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                return subprocess.CompletedProcess(args, 1, '',
                                                   'refusing to allow a GitHub App to create workflow without permission')
            return real_run(*args, **kwargs)

        with patch.object(self.git, 'run', side_effect=refuse_workflow):
            with self.assertRaisesRegex(m.SyncError, 'GITHUB_TOKEN cannot be granted workflows permission'):
                self.sync.apply(plan)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.snapshot(), before)

    def test_server_without_atomic_has_no_fallback(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        self.command('config', 'receive.advertiseAtomic', 'false', cwd=self.fork)
        plan = self.plan()
        before = self.snapshot()
        with patch.object(self.git, 'run', wraps=self.git.run) as run:
            with self.assertRaisesRegex(m.SyncError, 'does not support atomic'):
                self.sync.apply(plan)
        self.assertEqual(sum(call.args[0] == 'push' for call in run.call_args_list), 1)
        self.assertEqual(self.snapshot(), before)

    def test_post_push_verification_failure_reports_uncertain_no_retry(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        plan = self.plan()
        real_snapshot = self.git.snapshot
        fork_calls = 0

        def unavailable(endpoint):
            nonlocal fork_calls
            if endpoint == str(self.fork):
                fork_calls += 1
                if fork_calls == 2:
                    raise m.SyncError('unavailable')
            return real_snapshot(endpoint)

        with patch.object(self.git, 'snapshot', side_effect=unavailable):
            with self.assertRaisesRegex(m.SyncError, 'outcome uncertain'):
                self.sync.apply(plan)
        self.assertEqual(self.snapshot()['refs/tags/new'], self.base)

    def test_nonzero_after_success_is_verified_and_not_retried(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        plan = self.plan()
        real_run = self.git.run
        pushes = []

        def lost_ack(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[0] == 'push':
                pushes.append(args)
                return subprocess.CompletedProcess(result.args, 1, result.stdout, 'connection lost')
            return result

        with patch.object(self.git, 'run', side_effect=lost_ack):
            with self.assertRaisesRegex(m.SyncError, 'currently match the intended transaction'):
                self.sync.apply(plan)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.snapshot()['refs/tags/new'], self.base)

    def test_post_push_different_direct_object_is_uncertain(self):
        self.publish(self.upstream, 'refs/tags/new', self.base)
        plan = self.plan()
        real_run = self.git.run

        def concurrent_change(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[0] == 'push':
                self.publish(self.fork, 'refs/tags/new', self.tag_object('changed', self.base))
            return result

        with patch.object(self.git, 'run', side_effect=concurrent_change):
            with self.assertRaisesRegex(m.SyncError, 'direct-object verification differs'):
                self.sync.apply(plan)

    def test_invalid_expected_master_and_shallow_repository_rejected(self):
        for expected in ('master', '', '-x', 'A' * 40):
            with self.subTest(expected=expected), self.assertRaises(m.SyncError):
                self.sync.plan(expected)
        (self.work / '.git' / 'shallow').write_text(self.base + '\n')
        with self.assertRaisesRegex(m.SyncError, 'full-history'):
            self.plan()

    def test_production_repository_ref_and_remote_guards(self):
        env = {'GITHUB_REPOSITORY': m.REPOSITORY, 'GITHUB_REF': m.MASTER}
        url = 'https://github.com/' + m.REPOSITORY + '.git'
        self.command('remote', 'set-url', 'origin', url)
        m.validate_production(self.git, env)
        for bad_env in ({}, dict(env, GITHUB_REPOSITORY=m.UPSTREAM), dict(env, GITHUB_REF='refs/heads/other'),
                        dict(env, GITHUB_SERVER_URL='https://example.invalid')):
            with self.subTest(env=bad_env), self.assertRaises(m.SyncError):
                m.validate_production(self.git, bad_env)
        for bad in ('https://github.com/other/ardupilot.git', 'https://secret@github.com/' + m.REPOSITORY,
                    'https://github.com/' + m.REPOSITORY + '.git/extra', str(self.fork)):
            self.command('remote', 'set-url', '--push', 'origin', bad)
            with self.subTest(url=bad), self.assertRaises(m.SyncError):
                m.validate_production(self.git, env)
        self.command('remote', 'set-url', '--push', 'origin', url)
        self.command('remote', 'set-url', '--add', '--push', 'origin', 'https://example.invalid/extra')
        with self.assertRaises(m.SyncError):
            m.validate_production(self.git, env)

    def test_upstream_url_rewrite_rejected(self):
        self.command('remote', 'set-url', 'origin', 'https://github.com/' + m.REPOSITORY + '.git')
        self.command('config', 'url.https://example.invalid/.insteadOf', 'https://github.com/ArduPilot/')
        with self.assertRaisesRegex(m.SyncError, 'Upstream must resolve'):
            m.validate_production(self.git, {'GITHUB_REPOSITORY': m.REPOSITORY, 'GITHUB_REF': m.MASTER})


if __name__ == '__main__':
    unittest.main()
