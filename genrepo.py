#!/usr/bin/python -tt

from flask import Flask, request
import os.path
import subprocess
import threading
import urlparse

## FIXME:  These should go into a config file.
RPM_REPO_COMMIT_PATH = '/srv/release/repository/release/yum/builds/eucalyptus/commit/'
RPM_REPO_COMMIT_HTTP = 'http://192.168.51.243/yum/builds/eucalyptus/commit/'

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
    commit     = params.get('commit')
    branch     = params.get('branch')
    if distro.lower() in ['rhel', 'centos']:
        if commit:
            return find_rpm_repo_by_commit(distro, releasever, arch, url, commit)
        elif branch:
            return find_rpm_repo_by_branch(distro, releasever, arch, url, branch)
        else:
            return 'Error: either "commit" or "branch" must be specified', 400
    elif distro.lower() in ['debian', 'ubuntu']:
        if not commit:
            return 'Error: missing or empty parameter "commit"', 400
        return generate_deb_repo(distro, releasever, arch, url, commit)
    else:
        return 'Error: unknown distro "%s"' % distro, 400

def generate_deb_repo(distro, releasever, arch, url, commit):
    return 'Error: not implemented', 501

def find_rpm_repo_dir(commit):
    matches = [dir for dir in os.listdir(RPM_REPO_COMMIT_PATH)
               if dir.startswith(commit)]
    if len(matches) == 0:
        return (RPM_REPO_COMMIT_PATH, None)
    elif len(matches) == 1:
        return (RPM_REPO_COMMIT_PATH, matches[0])
    else:
        raise KeyError('Ref "%s" matches multiple package dir names' % commit)

def find_rpm_repo_by_commit(distro, releasever, arch, url, commit):
    # Quick sanity checks
    if arch == 'amd64':
        return 'Error: bad arch "amd64"; try "x86_64" instead', 400
    elif arch not in ['i386', 'x86_64']:
        return 'Error: bad arch "%s"' % arch, 400
    if distro == 'rhel' and any(releasever.startswith(n) for n in ('5', '6')):
        releasever = releasever[0]

    try:
        ref = resolve_git_ref(url, commit)
        (basedir, commitdir) = find_rpm_repo_dir(ref)
    except KeyError as err:
        return 'Error: %s' % err.msg, 412

    if commitdir:
        ospath = os.path.sep.join((distro, releasever, arch))
        if os.path.exists(os.path.join(basedir, commitdir, ospath)):
            cos_path = '/'.join((commitdir, ospath))
            return urlparse.urljoin(RPM_REPO_COMMIT_HTTP, cos_path), 200
        else:
            errmsg = ('Error: repo for commit %s exists, but not for %s' %
                      (commitdir, ospath))
            return errmsg, 404
    else:
        return 'Error: repo for ref %s does not exist' % ref, 404

def find_rpm_repo_by_branch(distro, releasever, arch, url, branch):
    # Quick sanity checks
    if arch == 'amd64':
        return 'Error: bad arch "amd64"; try "x86_64" instead', 400
    elif arch not in ['i386', 'x86_64']:
        return 'Error: bad arch "%s"' % arch, 400
    if distro == 'rhel' and any(releasever.startswith(n) for n in ('5', '6')):
        releasever = releasever[0]

    try:
        ref = resolve_git_ref(url, branch)
        (basedir, commitdir) = find_rpm_repo_dir(ref)
    except KeyError as err:
        return 'Error: %s' % err.msg, 412

    with BRANCH_COMMITS_LOCK:
        ospath = os.path.sep.join((distro, releasever, arch))
        if commitdir:
            # Try the latest commit
            if os.path.exists(os.path.join(basedir, commitdir, ospath)):
                BRANCH_COMMITS[branch] = ref
                cos_path = '/'.join((commitdir, ospath))
                return urlparse.urljoin(RPM_REPO_COMMIT_HTTP, cos_path), 200
        if branch in BRANCH_COMMITS:
            # Try the last known commit
            commit = BRANCH_COMMITS[branch]
            cos_path = '/'.join((commit, ospath))
            if os.path.exists(os.path.join(basedir, commit, ospath)):
                return urlparse.urljoin(RPM_REPO_COMMIT_HTTP, cos_path), 200
        return ('Error: no repo found for branch %s on platform %s' %
                (branch, ospath), 404)

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

def find_yum_repo(ref):
    matches = [dir for dir in os.listdir(RPM_REPO_BASE) if dir.startswith(ref)]
    if len(matches) == 0:
        return (RPM_REPO_BASE, None)
    elif len(matches) == 1:
        return (RPM_REPO_BASE, matches[0])
    else:
        raise KeyError('Ref "%s" matches multiple package dir names' % ref)

if __name__ == '__main__':
    app.debug = False
    app.run(host='0.0.0.0')
