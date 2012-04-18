#!/usr/bin/python -tt

from flask import Flask, request
import os.path
import subprocess
import urlparse

## FIXME:  These are guesses.
## FIXME:  They should also go into a config file.
JENKINS_TASK_DIR = '/var/lib/jenkins/tasks/{task}/artifacts/latest/{dist_rel}/{arch}'
RPM_REPO_BASE = '/srv/release/repository/release/yum/builds/eucalyptus/commit'
RPM_HTTP_BASE = 'http://FIXME/repository/release/yum/builds/eucalyptus/commit'

app = Flask(__name__)

@app.route('/genrepo/', methods=['GET', 'POST'])
def get_git_pkgs():
    if request.method == 'POST':
        print request.form
        for param in ['distro', 'release', 'arch', 'url', 'commit']:
            if not request.form.get(param):
                return ('Error: missing or empty parameter "%s"' % param, 400)
        distro  = request.form.get('distro')
        release = request.form.get('release')
        arch    = request.form.get('arch')
        url     = request.form.get('url')
        commit  = request.form.get('commit')
        if distro.lower() in ['rhel', 'centos']:
            return generate_yum_repo(distro, release, arch, url, commit)
        elif distro.lower() in ['debian', 'ubuntu']:
            return generate_deb_repo(distro, release, arch, url, commit)
        else:
            return 'Error: unknown distro "%s"' % distro, 400
    else:
        # Display a form for easy access
        return GET_GIT_PKG_FORM

def generate_deb_repo(distro, release, arch, url, commit):
    return 'Error: not implemented', 501

def generate_yum_repo(distro, release, arch, url, commit):
    # Quick sanity checks
    if arch == 'amd64':
        return 'Error: bad arch "amd64"; try "x86_64" instead', 400
    elif arch not in ['i386', 'x86_64']:
        return 'Error: bad arch "%s"' % arch, 400

    try:
        ref = resolve_git_ref(url, commit)
        (basedir, commitdir) = find_yum_repo(ref)
    except KeyError as err:
        return 'Error: %s' % err.msg, 412

    if commitdir:
        ospath = os.path.sep.join(distro, release, arch)
        if os.path.exists(os.path.join(basedir, commitdir, ospath)):
            return urlparse.urljoin(RPM_HTTP_BASE, commitdir, ospath), 200
        else:
            errmsg = ('Error: repo for commit %s exists, but not for %s' %
                      (commitdir, ospath))
            return errmsg, 404
    else:
        ## TODO:  copy artifacts
        ## TODO:  createrepo
        ## TODO:  what else?
        return 'Test', 404

def resolve_git_ref(url, ref):
    matches = set()
    cmdargs = ['git', 'ls-remote', url]
    gitcmd = subprocess.Popen(cmdargs, stdout=subprocess.PIPE)
    for line in gitcmd.stdout:
        (remotehash, remoteref) = line.split(None, 1)
        if any((remoteref == 'refs/heads/%s' % ref,
                remoteref == 'refs/tags/%s^{}' % ref)):
            matches.add(remotehash)
    assert subprocess.wait() == 0

    if len(matches) == 0:
        # Assume it's a hash we can use directly
        return ref
    if len(matches) == 1:
        return matches[0]
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

GET_GIT_PKG_FORM = '''\
<html>
    <h1>Generate a package repo</h1>
    <form method='post'>
        <p><b>Distro:</b>
            <input type='radio' name='distro' value='centos'>centos</input>
            <input type='radio' name='distro' value='debian'>debian</input>
            <input type='radio' name='distro' value='rhel'>rhel</input>
            <input type='radio' name='distro' value='ubuntu'>ubuntu</input>
        </p>
        <p><b>Release:</b> <input type='text' name='release'></p>
        <p><b>Arch:</b>
            <input type='radio' name='arch' value='i386'>i386</input>
            <input type='radio' name='arch' value='x86_64'>x86_64</input>
            <input type='radio' name='arch' value='amd64'>amd64</input>
        </p>
        <p><b>URL:</b> <input type='text' name='url'></p>
        <p><b>Git ref:</b> <input type='text' name='commit'></p>
        <p><input type='submit'/>
    </form>
</html>'''

if __name__ == '__main__':
    app.run()
