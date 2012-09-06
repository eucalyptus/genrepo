#!/usr/bin/python -tt

import apt
from flask import Flask, request
import os
import os.path
import subprocess
import threading
import time
import urlparse

# Since the only way we can have two commits with the same ID is by causing
# SHA1 to collide, we ignore that scenario and simply check each possible
# location in turn until we find one with a matching commit ID.
## FIXME:  This should go into a config file.
REPO_FS_BASE   = '/srv/release/repository/release'
REPO_HTTP_BASE = 'http://packages.release.eucalyptus-systems.com/'
RPM_FS_BASE    = os.path.join(REPO_FS_BASE, 'yum/builds')
RPM_HTTP_BASE  = urlparse.urljoin(REPO_HTTP_BASE, 'yum/builds/')

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
    ref        = params.get('ref')
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


def generate_deb_repo(distro, release, arch, url, commit=None, allow_old=False):
    if distro not in ['ubuntu', 'debian']:
        return "Error: Invalid distro.", 400
    if release not in ['lucid', 'precise', 'sid']:
        return "Error: Invalid release.", 400
    if url.endswith("eucalyptus"):
        package_name = "eucalyptus"
    elif url.endswith("internal"):
        package_name = "eucalyptus-enterprise"
    else:
        return ("Error: Invalid url.  Please end your URL with 'eucalyptus' "
                "or 'internal'"), 400

    # If no hash is specified, grab latest version and determine its hash
    if commit is None or commit == "master":
        sources = os.path.join(REPO_FS_BASE, distro, dists, release,
                               '/main/source/Sources.gz')
        f = gzip.open(sources, 'rb')
        data = f.read()
        f.close()
        latest_ver = None
        for section in data.split("\n\n"):
            if section.startswith("Package: " + package_name + "\n"):
                lines = section.split("\n")
                for line in lines:
                    if line.startswith("Version"):
                        key,val = line.split(": ")
                        latest_ver = val
                        break
        # We should have the latest version set at this point, but we won't if
        # the user specified an invalid distro or release. Bail!
        if latest_ver is None:
            return ('Error: Something wacky happened - unable to determine '
                    'package version'), 400
        # Parse the version to obtain the git hash.  This will be used for the
        # new repo name
        fields = latest_ver.split(".")
        commit = fields[-1][3:]

    # if QA passes us an entire commit ID, truncate
    elif len(commit) > 6:
        commit = commit[0:6]

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
