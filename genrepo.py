#!/usr/bin/python -tt

import apt
from flask import Flask, request
import os
import os.path
import shelve
import subprocess
import threading
import time
import urlparse

## FIXME:  This should go into a config file.
REPO_FS_BASE   = '/srv/release/repository/release'
REPO_HTTP_BASE = 'http://packages.release.eucalyptus-systems.com/'
RPM_FS_BASE    = os.path.join(REPO_FS_BASE, 'yum/builds')
RPM_HTTP_BASE  = urlparse.urljoin(REPO_HTTP_BASE, 'yum/builds/')
RESULT_CACHE_FILENAME = '/var/lib/genrepo/result-cache'

# A python shelf object:  the lazy man's key-value store
RESULT_CACHE = None
RESULT_CACHE_LOCK = threading.Lock()

app = Flask(__name__)

@app.route('/api/1/genrepo/', methods=['GET', 'POST'])
def genrepo_main():
    response_bits = list(get_git_pkgs())
    if len(response_bits) == 1:
        response_bits.append(200)
    if len(response_bits) == 2:
        response_bits.append({'Content-Type': 'text/plain'})

    if (isinstance(response_bits[0], basestring) and
        not response_bits[0].endswith('\n')):
        response_bits[0] += '\n'
    if isinstance(response_bits[0], list):
        response_bits[0] = '\n'.join(response_bits[0])
    return tuple(response_bits)


@app.route('/api/1/genrepo/cache/', methods=['GET', 'DELETE'])
def do_cache():
    if request.method == 'GET':
        cached_results = []
        for key, val in RESULT_CACHE['results'].items():
            cached_results.append(' '.join(key +
                    (str(val['atime']), str(val['mtime']), val['result'])))
        cached_results_str = '\n'.join(cached_results)
        if cached_results_str:
            cached_results_str += '\n'
        return cached_results_str, 200
    elif request.method == 'DELETE':
        with RESULT_CACHE_LOCK:
            RESULT_CACHE['results'].clear()
            RESULT_CACHE.sync()
        return '', 204


def get_git_pkgs():
    if request.method == 'POST':
        params = request.form
    elif request.method == 'GET':
        params = request.args
    for param in ['distro', 'releasever', 'arch', 'url']:
        if not params.get(param):
            return ('Error: missing or empty required parameter "%s"' % param,
                    400)
    distro     = params.get('distro')
    releasever = params.get('releasever')
    arch       = params.get('arch')
    url        = params.get('url')
    ref        = params.get('ref')
    allow_old  = 'allow-old' in params

    if ref:
        try:
            commit = resolve_git_ref(url, ref)
        except KeyError as exc:
            return 'Error: ' + exc.message, 412
    else:
        return 'Error: missing or empty parameter "ref"', 400

    if distro.lower() in ['rhel', 'centos']:
        msg, code = find_rpm_repo(distro, releasever, arch, url, commit)
    elif distro.lower() in ['debian', 'ubuntu']:
        msg, code = generate_deb_repo(distro, releasever, arch, url, commit)
    else:
        return 'Error: unknown distro "%s"' % distro, 400

    # Don't mess with the cache if ref was a commit ID
    if (any(char not in '01234567890abcdef' for char in ref.lower()) or
        not commit.startswith(ref.lower())):

        cache_key = (distro, releasever, arch, url, ref)
        with RESULT_CACHE_LOCK:
            if code <= 201:
                # Cache the result
                now = time.time()
                RESULT_CACHE['results'][cache_key] = {'atime': now,
                                                      'mtime': now,
                                                      'result': msg}
            elif code >= 400 and allow_old:
                # Try to use a cached result
                if cache_key in RESULT_CACHE['results']:
                    RESULT_CACHE['results'][cache_key]['atime'] = time.time()
                    return RESULT_CACHE['results'][cache_key]['result'], 200

    return msg, code


def generate_deb_repo(distro, release, arch, url, commit):
    if (distro, release) not in (('ubuntu', 'lucid'),
                                 ('ubuntu', 'precise'),
                                 ('debian', 'sid')):
        return 'Error: invalid release:  %s %s' % (distro, release), 400
    if url.endswith("eucalyptus"):
        package_name = "eucalyptus"
    elif url.endswith("internal"):
        package_name = "eucalyptus-enterprise"
    else:
        return ('Error: Invalid url.  Please end your URL with "eucalyptus" '
                'or "internal"'), 400

    # Truncate to 6 characters
    commit = commit[:6]

    # Locate debs
    pool = os.path.join(REPO_FS_BASE, distro, 'pool/main/e', package_name)
    pool_contents = os.listdir(pool)
    current_high_ver = "0"
    counter = 0
    for euca_file in pool_contents:
        if (commit in euca_file and euca_file.endswith('.deb') and
            release in euca_file):
            # Now determine the newest one
            fields = euca_file.split("_")
            euca_file_ver = fields[1]
            if apt.VersionCompare(euca_file_ver, current_high_ver) >= 1:
                current_high_ver = euca_file_ver
            counter += 1

    # eucalyptus has 10 binary packages (java-common may go away) and internal
    # has 4 + a dummy package if we have less than that, bail, as an invalid
    # hash has been detected
    if (package_name == 'eucalyptus' and counter < 9) or counter < 4:
        return ('Error: You have requested a commit that does not exist in '
                'this distro/release.'), 404

    # Generate the repository
    time.sleep(1)
    timestamp = str(int(time.time()))
    try:
        subprocess.check_call(['generate-eucalyptus-repository', distro, release,
                               commit + '-' + timestamp])
    except subprocess.CalledProcessError:
        return 'Error: failed to generate the repository', 500
    current_repo_name = release + "-" + commit + "-" + timestamp

    for euca_file in pool_contents:
        if (current_high_ver in euca_file and release in euca_file and
            euca_file.ends_with('.deb')):
            try:
                subprocess.check_call(
                        ['reprepro', '--keepunreferencedfiles', '-V', '-b',
                         os.path.join(REPO_FS_BASE, distro), 'includedeb',
                         current_repo_name, os.path.join(pool, euca_file)])
            except subprocess.CalledProcessError:
                return 'Error: failed to add DEBs to new repo', 500
    # Return the repo information
    return ' '.join(('deb', urlparse.urljoin(REPO_HTTP_BASE, 'ubuntu'),
                     'main')), 201


def find_rpm_repo_dirs(commit):
    matches = []
    for project in os.listdir(RPM_FS_BASE):
        path = os.path.join(RPM_FS_BASE, project, 'commit')
        matches.extend((dir, project) for dir in os.listdir(path)
                       if dir.startswith(commit))
    # Find the number of distinct commits that we matched
    n_matching_commits = len(set(match[0][:40] for match in matches))
    if n_matching_commits == 1:
        # All 'd1e524d09fab1e3498c84c26b264257496df6c4d-FOO', from last FOO
        # to first
        for build, project in sorted(matches, reverse=True):
            yield os.path.sep.join((project, 'commit', build))
    elif n_matching_commits > 1:
        raise KeyError('Ref "%s" matches multiple commits' % commit)
    # Fall through with no results


def find_rpm_repo(distro, releasever, arch, url, commit):
    # Quick sanity checks
    if arch == 'amd64':
        return 'Error: bad arch "amd64"; try "x86_64" instead', 400
    elif arch not in ['i386', 'x86_64']:
        return 'Error: bad arch "%s"' % arch, 400
    if distro == 'rhel' and any(releasever.startswith(n) for n in ('5', '6')):
        releasever = releasever[0]

    try:
        commit = resolve_git_ref(url, commit)
        commitdirs = find_rpm_repo_dirs(commit)
    except KeyError as err:
        return 'Error: %s' % err.msg, 412

    # Since the only way we can have two commits with the same ID is by causing
    # SHA1 to collide, we ignore that scenario and simply check each possible
    # location in turn until we find one with a matching commit ID.
    ospath = os.path.sep.join((distro, releasever, arch))
    for commitdir in commitdirs:
        if os.path.exists(os.path.join(RPM_FS_BASE, commitdir, ospath)):
            cos_path = '/'.join((commitdir, ospath))
            return urlparse.urljoin(RPM_HTTP_BASE, cos_path), 200
    return ('Error: no repo found for ref %s on platform %s' %
            (commit, ospath), 404)


def resolve_git_ref(url, ref):
    matches = set()
    cmdargs = ['git', 'ls-remote', url]
    gitcmd = subprocess.Popen(cmdargs, stdout=subprocess.PIPE)
    for line in gitcmd.stdout:
        (remotehash, remoteref) = line.strip().split(None, 1)
        if any((remoteref == 'refs/heads/%s' % ref,
                remoteref == 'refs/tags/%s^{}' % ref)):
            matches.add(remotehash)
    assert gitcmd.wait() == 0

    if len(matches) == 0:
        # Assume it's a hash we can use directly
        return ref
    if len(matches) == 1:
        return tuple(matches)[0]
    else:
        raise KeyError('Ref "%s" matches multiple objects' % ref)


def do_cache_upkeep():
    while True:
        time.sleep(300)
        with RESULT_CACHE_LOCK:
            if RESULT_CACHE is not None:
                # Clean out old results
                expiry = time.time() - 604800  # one week ago
                for entry in RESULT_CACHE['results'].keys():
                    if RESULT_CACHE['results'][entry]['atime'] < expiry:
                        del RESULT_CACHE['results'][entry]
                RESULT_CACHE.sync()


def setup_result_cache(filename):
    global RESULT_CACHE
    RESULT_CACHE = shelve.open(filename, writeback=True)

    if 'version' not in RESULT_CACHE:
        RESULT_CACHE['version'] = 1
    if 'results' not in RESULT_CACHE:
        RESULT_CACHE['results'] = {}

    cache_upkeep_thread = threading.Thread(target=do_cache_upkeep)
    cache_upkeep_thread.daemon = True
    cache_upkeep_thread.start()


if __name__ == '__main__':
    app.debug = True
    try:
        setup_result_cache(RESULT_CACHE_FILENAME)
        app.run(host='0.0.0.0')
    finally:
        with RESULT_CACHE_LOCK:
            if RESULT_CACHE:
                RESULT_CACHE.close()
                RESULT_CACHE = None
