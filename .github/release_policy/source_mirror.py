"""Mirror formal upstream source releases only; never build/upload firmware. AP_FLAKE8_CLEAN"""
import hashlib
import json
import os
import subprocess

from pathlib import Path

import policy

REPOSITORY = 'reliable-ly0411/ardupilot'
UPSTREAM = 'ArduPilot/ardupilot'


def api(path, payload=None):
    args = ['gh', 'api', path]
    if payload is not None:
        args += ['--method', 'POST', '--input', '-']
    return json.loads(subprocess.check_output(args, input=None if payload is None else json.dumps(payload), text=True))


def release_body(release, sha):
    return (f"Source-only mirror of https://github.com/{UPSTREAM}/releases/tag/{release['tag_name']}\n\n"
            "**源码镜像 / Source only. No compiled firmware is provided.**\n\n"
            "GitHub's source archives contain the unchanged upstream tagged source and its original license files. "
            "Submodules are references, not bundled dependencies; clone recursively for a complete checkout.\n\n"
            f"Upstream release ID: {release['id']}\nUpstream commit: `{sha}`\n"
            f"Upstream publication: {release['published_at']}\n\n---\n\n"
            + (release.get('body') or ''))


def verify_release(release, entry, rid):
    if (str(release['id']) != rid or release['draft'] or release['prerelease']
            or release['tag_name'] != entry['tag'] or release['published_at'] != entry['published_at']):
        raise ValueError('Upstream formal release identity changed')
    if entry.get('notes_sha256') and notes_digest(release) != entry['notes_sha256']:
        raise ValueError('Upstream release notes changed after recognition')


def notes_digest(release):
    return hashlib.sha256(json.dumps([release.get('name'), release.get('body')],
                                     ensure_ascii=False).encode()).hexdigest()


def mirror(config, rid, entry):
    tag = entry['tag']
    release = api(f'repos/{UPSTREAM}/releases/{rid}')
    verify_release(release, entry, rid)
    sha = policy.resolve_tag(config, tag)
    if sha != entry['upstream_sha']:
        raise ValueError('Upstream tag moved')
    tag_object = policy.git('rev-parse', '--verify', 'FETCH_HEAD')
    tag_ref = 'refs/tags/' + tag
    # The unchanged commit supplies original licensing in GitHub source archives.
    license_text = policy.git('show', sha + ':COPYING.txt')
    if 'GNU GENERAL PUBLIC LICENSE' not in license_text:
        raise ValueError('Original source license is missing or unrecognized')
    refs = policy.git('ls-remote', '--tags', 'origin', tag_ref, tag_ref + '^{}')
    existing = {}
    for line in refs.splitlines():
        value, name = line.split()
        existing[name] = value
    peeled = existing.get(tag_ref + '^{}', existing.get(tag_ref))
    if peeled and (peeled != sha or existing.get(tag_ref) != tag_object):
        raise ValueError('Downstream same-name tag differs; never overwrite')
    body = release_body(release, sha)
    pages = json.loads(policy.command('gh', 'api', '--paginate', '--slurp',
                                      f'repos/{REPOSITORY}/releases?per_page=100'))
    releases = [r for page in pages for r in page]
    matching = [r for r in releases if r['tag_name'] == tag]
    if matching:
        if (len(matching) != 1 or matching[0]['draft'] or matching[0]['prerelease']
                or matching[0]['body'] != body or matching[0].get('assets')
                or matching[0].get('name') != (release.get('name') or tag)):
            raise ValueError('Existing downstream release differs; never overwrite')
        if not peeled:
            raise ValueError('Existing release is missing its tag')
        return matching[0]['html_url']
    # Recheck immediately before publication; a changed upstream ID/tag fails closed.
    verify_release(api(f'repos/{UPSTREAM}/releases/{rid}'), entry, rid)
    if (policy.resolve_tag(config, tag) != sha
            or policy.git('rev-parse', '--verify', 'FETCH_HEAD') != tag_object):
        raise ValueError('Upstream tag changed before publication')
    if not peeled:
        # A formal tag can appear after the all-ref sync snapshot. Preserve its
        # original annotated object and require that the destination is absent.
        policy.git('push', '--atomic', '--force-with-lease=' + tag_ref + ':',
                   'origin', tag_object + ':' + tag_ref)
    current = policy.git('ls-remote', '--refs', 'origin', tag_ref).splitlines()
    if len(current) != 1 or current[0].split() != [tag_object, tag_ref]:
        raise ValueError('Downstream tag changed before source publication')
    result = api(f'repos/{REPOSITORY}/releases', {
        'tag_name': tag, 'name': release.get('name') or tag, 'body': body,
        'draft': False, 'prerelease': False, 'make_latest': 'false',
    })
    return result['html_url']


def main():
    if os.environ['GITHUB_REPOSITORY'] != REPOSITORY:
        raise ValueError('Source mirror is restricted to the authorized fork')
    config = policy.load_config()
    if config['upstream'] != UPSTREAM:
        raise ValueError('Unexpected upstream')
    state, parent = policy.load_state(config)
    pages = json.loads(policy.command('gh', 'api', '--paginate', '--slurp', f'repos/{UPSTREAM}/releases?per_page=100'))
    releases = [r for page in pages for r in page]
    retry = os.environ.get('RETRY_ID', '') or None
    if retry and state['releases'].get(retry, {}).get('status') not in ('failed', 'building'):
        raise ValueError('Only failed/incomplete recognized releases may be retried')
    selected = policy.select_release(releases, state, config, lambda tag: policy.resolve_tag(config, tag), retry)
    if not selected:
        policy.save_state(state, parent)
        return
    rid, entry = selected
    release = next(r for r in releases if str(r['id']) == rid)
    entry.setdefault('notes_sha256', notes_digest(release))
    entry.update(status='building', attempts=entry.get('attempts', 0) + 1,
                 run_id=os.environ['GITHUB_RUN_ID'], publication='source-only GitHub Release; no firmware')
    policy.save_state(state, parent)
    state, parent = policy.load_state(config)
    entry = state['releases'][rid]
    try:
        url = mirror(config, rid, entry)
    except Exception:
        entry['status'] = 'failed'
        policy.save_state(state, parent)
        raise
    entry.update(status='built', release_url=url)
    policy.save_state(state, parent)
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as handle:
        handle.write(f'Source-only release mirrored: {url}. No firmware built or uploaded.\n')


if __name__ == '__main__':
    main()
