"""Conservative, atomic upstream branch/tag synchronization. AP_FLAKE8_CLEAN

The CLI is restricted to this fork on GitHub Actions master. SyncRefs accepts
local Git endpoints only as a Python testing seam, never as workflow inputs.
Fetched objects live in a fresh refs/sync-refs namespace, not local branches or
tags. No checked-out upstream code, hooks, or submodules are executed.
"""
import argparse
import os
import re
import subprocess
import sys
import uuid

from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

REPOSITORY = 'reliable-ly0411/ardupilot'
UPSTREAM = 'ArduPilot/ardupilot'
UPSTREAM_URL = 'https://github.com/' + UPSTREAM + '.git'
MASTER = 'refs/heads/master'
ROLLING_TAGS = frozenset({
    'APMrover2-beta', 'APMrover2-stable', 'AP_Periph-beta', 'AP_Periph-stable',
    'AntennaTracker-beta', 'AntennaTracker-stable', 'ArduCopter-beta', 'ArduCopter-beta-heli',
    'ArduCopter-stable', 'ArduCopter-stable-heli', 'ArduPlane-beta', 'ArduPlane-stable',
    'ArduSub-beta', 'ArduSub-stable',
})
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


class SyncError(RuntimeError):
    """A failed precondition or a transaction requiring operator inspection."""


class Git:
    def __init__(self, directory):
        self.directory = str(directory)

    def run(self, *args, check=True):
        env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_TERMINAL_PROMPT='0')
        result = subprocess.run(
            ['git', '-C', self.directory, '-c', 'core.hooksPath=/dev/null',
             '-c', 'core.fsmonitor=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
             '-c', 'fetch.prune=false', '-c', 'fetch.pruneTags=false', '-c', 'fetch.recurseSubmodules=false',
             '-c', 'push.followTags=false', '-c', 'remote.origin.mirror=false', '-c', 'push.gpgSign=false',
             '-c', 'protocol.ext.allow=never', *args],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if check and result.returncode:
            # Never echo Git's remote output: URLs and credential-helper errors can contain secrets.
            raise SyncError('Git operation failed; remote output withheld. No automatic retry was attempted.')
        return result

    def text(self, *args):
        return self.run(*args).stdout.strip()

    def snapshot(self, endpoint):
        output = self.text('ls-remote', '--refs', '--heads', '--tags', endpoint)
        refs = {}
        for line in output.splitlines():
            parts = line.split('\t')
            if len(parts) != 2 or not OID.fullmatch(parts[0]):
                raise SyncError('Malformed advertised ref snapshot')
            oid, ref = parts
            if (not ref.startswith(('refs/heads/', 'refs/tags/')) or ref in refs
                    or self.run('check-ref-format', ref, check=False).returncode):
                raise SyncError('Invalid or duplicate advertised ref')
            refs[ref] = oid
        return refs

    def fetch_snapshot(self, endpoint, refs, namespace):
        if not refs:
            return
        refspecs = [f'{ref}:{namespace}/{ref[5:]}' for ref in sorted(refs)]
        self.run('fetch', '--no-tags', '--no-prune', '--no-prune-tags', '--no-recurse-submodules',
                 '--no-write-fetch-head', '--no-auto-maintenance', '--refmap=', endpoint, *refspecs)
        fetched = {}
        output = self.text('for-each-ref', '--format=%(objectname)\t%(refname)', namespace + '/')
        for line in output.splitlines():
            oid, ref = line.split('\t')
            fetched['refs/' + ref[len(namespace) + 1:]] = oid
        if fetched != refs:
            raise SyncError('Advertised refs changed during fetch; no remote writes attempted')

    def ancestor(self, older, newer):
        result = self.run('merge-base', '--is-ancestor', older, newer, check=False)
        if result.returncode not in (0, 1):
            raise SyncError('Could not prove branch ancestry; no remote writes attempted')
        return result.returncode == 0


def validate_production(git, env):
    if env.get('GITHUB_REPOSITORY') != REPOSITORY or env.get('GITHUB_REF') != MASTER:
        raise SyncError('Ref sync is restricted to reliable-ly0411/ardupilot on refs/heads/master')
    if env.get('GITHUB_SERVER_URL', 'https://github.com') != 'https://github.com':
        raise SyncError('Ref sync requires github.com')
    allowed = {'https://github.com/' + REPOSITORY, 'https://github.com/' + REPOSITORY + '.git'}
    for options in (('--all',), ('--push', '--all')):
        urls = git.text('remote', 'get-url', *options, 'origin').splitlines()
        if len(urls) != 1 or urls[0] not in allowed:
            raise SyncError('Origin must resolve only to the authorized HTTPS GitHub fork, without embedded credentials')
    # Check URL rewrites too, without reading or printing credential configuration.
    urls = git.text('ls-remote', '--get-url', UPSTREAM_URL).splitlines()
    if urls != [UPSTREAM_URL]:
        raise SyncError('Upstream must resolve exactly to the fixed ArduPilot HTTPS URL')


@dataclass(frozen=True)
class Update:
    ref: str
    old: str
    new: str
    reason: str


@dataclass
class Plan:
    upstream: dict
    fork: dict
    head: str
    updates: dict = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    fork_ahead: list = field(default_factory=list)
    fork_only: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)

    def add(self, ref, old, new, reason):
        update = Update(ref, old, new, reason)
        previous = self.updates.get(ref)
        if previous and (previous.old, previous.new) != (old, new):
            self.conflicts.append('Conflicting planned values for ' + ref)
        elif not previous:
            self.updates[ref] = update
            self.counts[reason] += 1

    def summary(self):
        lines = [f'Upstream snapshot: {sum(r.startswith("refs/heads/") for r in self.upstream)} branches, '
                 f'{sum(r.startswith("refs/tags/") for r in self.upstream)} tags.',
                 f'Planned changed refs: {len(self.updates)}; conflicts: {len(self.conflicts)}.']
        lines.extend(f'{kind}: {count}' for kind, count in sorted(self.counts.items()))
        groups = [
            ('New refs', [u.ref for u in self.updates.values() if not u.old and u.reason != 'new backup']),
            ('Preserved fork-ahead branches', self.fork_ahead),
            ('Preserved fork-only branches', self.fork_only),
            ('Old direct-object backups', [u.ref for u in self.updates.values() if u.reason == 'new backup']),
            ('Conflicts', self.conflicts),
        ]
        for label, names in groups:
            lines.append(f'{label}: {len(names)}')
            lines.extend('  ' + name for name in sorted(names)[:20])
            if len(names) > 20:
                lines.append(f'  ... and {len(names) - 20} more')
        return '\n'.join(lines)


class SyncRefs:
    def __init__(self, git, upstream=UPSTREAM_URL, fork='origin'):
        self.git = git
        self.upstream = upstream
        self.fork = fork

    def plan(self, expected_master):
        if not OID.fullmatch(expected_master):
            raise SyncError('Expected master must be a full lowercase object ID')
        if self.git.text('rev-parse', '--is-shallow-repository') != 'false':
            raise SyncError('A full-history checkout is required to prove branch ancestry')
        head = self.git.text('rev-parse', '--verify', 'HEAD^{commit}')
        upstream = self.git.snapshot(self.upstream)
        fork = self.git.snapshot(self.fork)
        plan = Plan(upstream, fork, head)
        if MASTER not in upstream:
            plan.conflicts.append('Upstream master is missing')
        if fork.get(MASTER) != expected_master:
            plan.conflicts.append('Remote master differs from expected pre-merge master')
        if plan.conflicts:
            return plan
        transaction = uuid.uuid4().hex
        namespace = 'refs/sync-refs/' + transaction
        self.git.fetch_snapshot(self.upstream, upstream, namespace + '/upstream')
        self.git.fetch_snapshot(self.fork, fork, namespace + '/fork')
        if not self.git.ancestor(expected_master, head) or not self.git.ancestor(upstream[MASTER], head):
            plan.conflicts.append('Prepared master must contain both expected fork master and fetched upstream master')
        elif head != expected_master:
            plan.add(MASTER, expected_master, head, 'prepared master')
        else:
            plan.counts['unchanged branches'] += 1
        for ref, new in sorted(upstream.items()):
            if ref == MASTER:
                continue
            old = fork.get(ref, '')
            if ref.startswith('refs/heads/'):
                if not old:
                    plan.add(ref, '', new, 'new branches')
                elif old == new:
                    plan.counts['unchanged branches'] += 1
                elif self.git.ancestor(old, new):
                    plan.add(ref, old, new, 'fast-forward branches')
                elif self.git.ancestor(new, old):
                    plan.fork_ahead.append(ref)
                else:
                    plan.conflicts.append('Divergent branch: ' + ref)
            elif not old:
                plan.add(ref, '', new, 'new tags')
            elif old == new:
                plan.counts['unchanged tags'] += 1
            elif ref[len('refs/tags/'):] not in ROLLING_TAGS:
                plan.conflicts.append('Fixed tag direct-object conflict: ' + ref)
            else:
                backup = 'refs/tags/sync-backup/rolling/' + ref[len('refs/tags/'):] + '/' + old + '-' + transaction
                if backup in fork or backup in upstream:
                    plan.conflicts.append('Fresh backup ref already exists: ' + backup)
                    continue
                # A fresh ref ensures receive-pack checks an actual create, not an omitted no-op lease.
                plan.add(backup, '', old, 'new backup')
                plan.add(ref, old, new, 'rolling tags')
        plan.fork_only = sorted(r for r in fork if r.startswith('refs/heads/') and r not in upstream)
        plan.counts['preserved fork-only tags'] = sum(r.startswith('refs/tags/') and r not in upstream for r in fork)
        final_refs = sorted(set(fork) | set(plan.updates))
        final_set = set(final_refs)
        for ref in final_refs:
            parts = ref.split('/')
            for end in range(2, len(parts)):
                parent = '/'.join(parts[:end])
                if parent in final_set:
                    plan.conflicts.append(f'Ref namespace collision: {parent} blocks {ref}')
        return plan

    def apply(self, plan):
        if plan.conflicts:
            raise SyncError('Plan has conflicts; no remote writes attempted')
        if self.git.text('rev-parse', '--verify', 'HEAD^{commit}') != plan.head:
            raise SyncError('Prepared local master changed; no remote writes attempted')
        if self.git.snapshot(self.upstream) != plan.upstream:
            raise SyncError('Upstream refs changed before push; no remote writes attempted')
        if self.git.snapshot(self.fork) != plan.fork:
            raise SyncError('Fork refs changed before push; no remote writes attempted')
        if not plan.updates:
            return 'No remote changes needed; snapshots rechecked.'
        updates = list(plan.updates.values())
        leases = [f'--force-with-lease={u.ref}:{u.old}' for u in updates]
        refspecs = [f'{u.new}:{u.ref}' for u in updates]
        result = self.git.run('push', '--porcelain', '--atomic', '--no-follow-tags', '--no-verify', '--signed=false',
                              *leases, self.fork, *refspecs, check=False)
        try:
            after = self.git.snapshot(self.fork)
        except SyncError as error:
            raise SyncError('Push outcome uncertain: post-push verification failed. Inspect remote refs before any retry; '
                            'no automatic retry was attempted.') from error
        matching = sum(after.get(u.ref) == u.new for u in updates)
        if result.returncode:
            reason = 'Atomic push failed or was rejected'
            if 'does not support --atomic push' in result.stderr:
                reason = 'Server does not support atomic push'
            remote_error = (result.stdout + result.stderr).lower()
            if 'workflow' in remote_error and any(word in remote_error for word in
                                                  ('permission', 'refusing to allow', 'scope')):
                reason = ('GitHub rejected workflow-file updates for this token; GITHUB_TOKEN cannot be granted '
                          'workflows permission in workflow YAML')
            if all(after.get(u.ref, '') == u.old for u in updates):
                outcome = 'Remote planned refs remain at their expected old values.'
            elif matching == len(updates):
                outcome = 'Remote refs currently match the intended transaction, but Git reported failure.'
            else:
                outcome = 'Remote state differs from the original and intended transaction; outcome is uncertain.'
            raise SyncError(f'{reason}. {outcome} Inspect before retrying; no fallback or automatic retry was attempted.')
        if matching != len(updates):
            raise SyncError('Push reported success but direct-object verification differs; outcome is uncertain. '
                            'Inspect remote refs before any retry; no automatic retry was attempted.')
        return f'Atomic push verified: {len(plan.updates)} changed refs, exact direct object IDs confirmed.'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-master', required=True, help='Full remote master OID recorded before merge preparation')
    parser.add_argument('--apply', action='store_true', help='Apply one atomic transaction; default only prepares a plan')
    args = parser.parse_args(argv)
    try:
        git = Git(Path.cwd())
        validate_production(git, os.environ)
        sync = SyncRefs(git)
        plan = sync.plan(args.expected_master)
        print(plan.summary())
        if plan.conflicts:
            raise SyncError('Conflicts block the entire transaction; no remote writes attempted')
        if args.apply:
            # Revalidate the production target before any remote writes.
            validate_production(git, os.environ)
            print(sync.apply(plan))
        else:
            print('Plan only: no remote writes attempted. Use --apply to execute the transaction.')
    except SyncError as error:
        print('Ref sync stopped: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
