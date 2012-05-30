#!/usr/bin/python -tt

from flask import Flask, request
import os.path
import subprocess
import threading
import urlparse

# Since the only way we can have two commits with the same ID is by causing
# SHA1 to collide, we ignore that scenario and simply check each possible
# location in turn until we find one with a matching commit ID.
## FIXME:  This should go into a config file.
RPM_FS_BASE   = '/srv/release/repository/release/yum/builds'
RPM_HTTP_BASE = 'http://192.168.51.243/yum/builds/'

BRANCH_COMMITS = {}
BRANCH_COMMITS_LOCK = threading.Lock()

app = Flask(__name__)

@app.route('/genrepo/', methods=['GET', 'POST'])
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
    ref        = params.get('ref') or params.get('commit') or params.get('branch')
    allow_old  = 'allow-old' in params
    if distro.lower() in ['rhel', 'centos']:
        if ref:
            return find_rpm_repo(distro, releasever, arch, url, ref, allow_old)
        else:
            return 'Error: missing or empty paramster "ref"', 400
    elif distro.lower() in ['debian', 'ubuntu']:
        if ref:
            return generate_deb_repo(distro, releasever, arch, url, ref,
                                     allow_old)
        else:
            return 'Error: missing or empty parameter "ref"', 400
    else:
        return 'Error: unknown distro "%s"' % distro, 400

def generate_deb_repo(distro, releasever, arch, url, ref, allow_old=False):
    return 'Error: not implemented', 501

def find_rpm_repo_dir(commit):
    matches = []
    for project in os.listdir(RPM_FS_BASE):
        path = os.path.join(RPM_FS_BASE, project, 'commit')
        matches.extend((dir, project) for dir in os.listdir(path)
                       if dir.startswith(commit))
    # Find the number of distinct commits that we matched
    n_matching_commits = len(set(match[0][:40] for match in matches))
    if n_matching_commits == 1:
        # latest NNN of all 'd1e524d09fab1e3498c84c26b264257496df6c4d-NNN'
        (latest_matching_build, project) = sorted(matches)[-1]
        return os.path.sep.join((project, 'commit', latest_matching_build))
    elif n_matching_commits > 1:
        raise KeyError('Ref "%s" matches multiple commits' % commit)
    return None

def find_rpm_repo(distro, releasever, arch, url, ref, allow_old=False):
    # Quick sanity checks
    if arch == 'amd64':
        return 'Error: bad arch "amd64"; try "x86_64" instead', 400
    elif arch not in ['i386', 'x86_64']:
        return 'Error: bad arch "%s"' % arch, 400
    if distro == 'rhel' and any(releasever.startswith(n) for n in ('5', '6')):
        releasever = releasever[0]

    try:
        commit = resolve_git_ref(url, ref)
        commitdir = find_rpm_repo_dir(commit)
    except KeyError as err:
        return 'Error: %s' % err.msg, 412

    with BRANCH_COMMITS_LOCK:
        ospath = os.path.sep.join((distro, releasever, arch))
        if commitdir:
            # Try the latest commit
            if os.path.exists(os.path.join(RPM_FS_BASE, commitdir, ospath)):
                BRANCH_COMMITS[ref] = commitdir
                cos_path = '/'.join((commitdir, ospath))
                return urlparse.urljoin(RPM_HTTP_BASE, cos_path), 200
        elif allow_old and ref in BRANCH_COMMITS:
            # Try the last known commit
            commitdir = BRANCH_COMMITS[ref]
            cos_path = '/'.join((commitdir, ospath))
            if os.path.exists(os.path.join(basedir, cos_path)):
                return urlparse.urljoin(RPM_HTTP_BASE, cos_path), 200
        return ('Error: no repo found for ref %s on platform %s' %
                (ref, ospath), 404)

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

if __name__ == '__main__':
    app.debug = False
    app.run(host='0.0.0.0')
