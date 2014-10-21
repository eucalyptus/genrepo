#!/usr/bin/python -tt
#
# Copyright 2009-2014 Eucalyptus Systems, Inc.
#
# Redistribution and use of this software in source and binary forms, with or
# without modification, are permitted provided that the following conditions
# are met:
#
#   Redistributions of source code must retain the above
#   copyright notice, this list of conditions and the
#   following disclaimer.
#
#   Redistributions in binary form must reproduce the above
#   copyright notice, this list of conditions and the
#   following disclaimer in the documentation and/or other
#   materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import apt
from flask import Flask, request
import os
import os.path
import re
import shelve
import subprocess
import threading
import time
import urlparse

## FIXME:  This should go into a config file.
REPO_FS_BASE   = '/srv/release/repository/release'
REPO_HTTP_BASE = 'http://packages.release.eucalyptus-systems.com/'
YUM_BASE       = 'yum/builds/'
RPM_FS_BASE    = os.path.join(REPO_FS_BASE, YUM_BASE)
RPM_HTTP_BASE  = urlparse.urljoin(REPO_HTTP_BASE, YUM_BASE)
RESULT_CACHE_FILENAME = '/var/lib/genrepo/result-cache'

# A python shelf object:  the lazy man's key-value store
RESULT_CACHE = None
RESULT_CACHE_LOCK = threading.Lock()

app = Flask(__name__)


@app.route('/api/1/genrepo/', methods=['GET', 'POST'])
def do_genrepo():
    if request.method == 'POST':
        params = request.form
    elif request.method == 'GET':
        params = request.args
    for param in ['distro', 'releasever', 'arch', 'url']:
        if not params.get(param):
            return format_plaintext_response(
                    'Error: missing or empty required parameter "%s"' % param,
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
            return format_plaintext_response('Error: ' + exc.message, 412)
    else:
        return format_plaintext_response(
                'Error: missing or empty parameters "ref"', 400)

    url = normalize_git_url(url)

    msg, code = get_git_pkgs(distro, releasever, arch, url, commit)

    # Don't mess with the cache if ref was a commit ID
    if (any(char not in '01234567890abcdef' for char in ref.lower()) or
        not commit.startswith(ref.lower())):

        if code <= 201:
            # Cache the result
            update_cache(distro, releasever, arch, url, ref, msg)
        elif code >= 400 and allow_old:
            # Try to use a cached result
            cached_msg = check_cache(distro, releasever, arch, url, ref)
            if cached_msg is not None:
                msg  = cached_msg
                code = 200
    return format_plaintext_response(msg, code)


@app.route('/api/1/genrepo/cache/', methods=['GET', 'DELETE', 'PUT'])
def do_genrepo_cache():
    if request.method == 'GET':
        cached_results = []
        for key, val in RESULT_CACHE['results'].items():
            cached_results.append(' '.join(key +
                    (str(val['atime']), str(val['mtime']), val['result'])))
        return format_plaintext_response(cached_results, 200)
    elif request.method == 'DELETE':
        with RESULT_CACHE_LOCK:
            RESULT_CACHE['results'].clear()
            RESULT_CACHE.sync()
        return format_plaintext_response('', 204)
    elif request.method == 'PUT':
        for param in ['distro', 'releasever', 'arch', 'url', 'ref', 'commit']:
            if not request.form.get(param):
                return format_plaintext_response(
                        ('Error: missing or empty required parameter '
                         '"%s"') % param, 400)
        distro     = request.form.get('distro')
        releasever = request.form.get('releasever')
        arch       = request.form.get('arch')
        url        = request.form.get('url')
        ref        = request.form.get('ref')
        commit     = request.form.get('commit')

        if len(commit) != 40 or not all(c in '0123456789abcdef' for c in commit):
            return format_plaintext_response(
                    'Error: commit must be a 40-character commit hash', 400)

        url = normalize_git_url(url)

        msg, code = get_git_pkgs(distro, releasever, arch, url, commit)
        if code < 300:
            update_cache(distro, releasever, arch, url, ref, msg)
            return format_plaintext_response('', 204)
        else:
            return format_plaintext_response(msg, code)


def get_git_pkgs(distro, releasever, arch, url, commit):
    if distro.lower() in ['rhel', 'centos']:
        return find_rpm_repo(distro, releasever, arch, url, commit)
    elif distro.lower() in ['debian', 'ubuntu']:
        return generate_deb_repo(distro, releasever, arch, url, commit)
    else:
        return 'Error: unknown distro "%s"' % distro, 400


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
            euca_file.endswith('.deb')):
            try:
                subprocess.check_call(
                        ['reprepro', '--keepunreferencedfiles', '-V', '-b',
                         os.path.join(REPO_FS_BASE, distro), 'includedeb',
                         current_repo_name, os.path.join(pool, euca_file)])
            except subprocess.CalledProcessError:
                return 'Error: failed to add DEBs to new repo', 500
    # Return the repo information
    return ' '.join(('deb', urlparse.urljoin(REPO_HTTP_BASE, distro),
                     current_repo_name, 'main')), 201


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


def normalize_git_url(url):
    # Normalize the URL
    # Don't forget that re.match only searches from the start of the string.
    if url.endswith('.git'):
        url = url[:-4]
    match = re.match('([^@]+)@([^:]+):(.*)', url)
    if match:
        groups = match.groups()
        return 'git+ssh://{0}@{1}/{2}'.format(groups[0], groups[1], groups[2])
    match = re.match('ssh://[^@]+@.+/.*', url)
    if match:
        return 'git+' + url
    return url


def format_plaintext_response(msg=None, code=None, headers=None):
    if msg is None:
        msg = ''
    elif isinstance(msg, basestring) and not msg.endswith('\n'):
        msg += '\n'
    elif isinstance(msg, list):
        msg = '\n'.join(msg)
        if msg:
            msg += '\n'

    if code is None:
        code = 200

    if headers is None:
        headers = {'Content-Type': 'text/plain'}

    return (msg, code, headers)


def update_cache(distro, releasever, arch, url, ref, result):
    cache_key = (distro, releasever, arch, url, ref)
    with RESULT_CACHE_LOCK:
        now = time.time()
        RESULT_CACHE['results'][cache_key] = {'atime': now,
                                              'mtime': now,
                                              'result': result}
        RESULT_CACHE.sync()


def check_cache(distro, releasever, arch, url, ref):
    cache_key = (distro, releasever, arch, url, ref)
    with RESULT_CACHE_LOCK:
        if cache_key in RESULT_CACHE['results']:
            RESULT_CACHE['results'][cache_key]['atime'] = time.time()
            return RESULT_CACHE['results'][cache_key]['result']
    return None


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

    migrate_0_1()
    migrate_1_2()

    cache_upkeep_thread = threading.Thread(target=do_cache_upkeep)
    cache_upkeep_thread.daemon = True
    cache_upkeep_thread.start()


def migrate_0_1():
    if 'version' not in RESULT_CACHE or RESULT_CACHE['version'] < 1:
        print 'Creating new database'
        if 'version' not in RESULT_CACHE:
            RESULT_CACHE['version'] = 1
        if 'results' not in RESULT_CACHE:
            RESULT_CACHE['results'] = {}


def migrate_1_2():
    if RESULT_CACHE['version'] == 1:
        print 'Migrating database from version 1 to 2'
        for old_key in RESULT_CACHE['results'].keys():
            # Normalize the URL
            # Don't forget that re.match only searches from the start of the string.
            old_url = old_key[3]
            new_url = normalize_git_url(old_url)
            if new_url != old_url:
                print 'Migrating', old_url, 'to', new_url
                new_key = list(old_key)
                new_key[3] = new_url
                new_key = tuple(new_key)
                RESULT_CACHE['results'][new_key] = RESULT_CACHE['results'][old_key]
                del RESULT_CACHE['results'][old_key]
    RESULT_CACHE['version'] = 2


if __name__ == '__main__':
    app.debug = False
    try:
        setup_result_cache(RESULT_CACHE_FILENAME)
        app.run(host='0.0.0.0')
    finally:
        with RESULT_CACHE_LOCK:
            if RESULT_CACHE:
                RESULT_CACHE.close()
                RESULT_CACHE = None
