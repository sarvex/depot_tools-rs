# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""SCM-specific utility classes."""

import distutils.version
import glob
import io
import os
import platform
import re
import sys

import gclient_utils
import subprocess2


def ValidateEmail(email):
  return (
      re.match(r"^[a-zA-Z0-9._%\-+]+@[a-zA-Z0-9._%-]+.[a-zA-Z]{2,6}$", email)
      is not None)


def GetCasedPath(path):
  """Elcheapos way to get the real path case on Windows."""
  if sys.platform.startswith('win') and os.path.exists(path):
    # Reconstruct the path.
    path = os.path.abspath(path)
    paths = path.split('\\')
    for i in range(len(paths)):
      if i == 0:
        # Skip drive letter.
        continue
      subpath = '\\'.join(paths[:i+1])
      prev = len('\\'.join(paths[:i]))
      # glob.glob will return the cased path for the last item only. This is why
      # we are calling it in a loop. Extract the data we want and put it back
      # into the list.
      paths[i] = glob.glob(f'{subpath}*')[0][prev+1:len(subpath)]
    path = '\\'.join(paths)
  return path


def GenFakeDiff(filename):
  """Generates a fake diff from a file."""
  file_content = gclient_utils.FileRead(filename, 'rb').splitlines(True)
  filename = filename.replace(os.sep, '/')
  nb_lines = len(file_content)
  # We need to use / since patch on unix will fail otherwise.
  data = io.StringIO()
  data.write("Index: %s\n" % filename)
  data.write('=' * 67 + '\n')
  # Note: Should we use /dev/null instead?
  data.write("--- %s\n" % filename)
  data.write("+++ %s\n" % filename)
  data.write("@@ -0,0 +1,%d @@\n" % nb_lines)
  # Prepend '+' to every lines.
  for line in file_content:
    data.write('+')
    data.write(line)
  result = data.getvalue()
  data.close()
  return result


def determine_scm(root):
  """Similar to upload.py's version but much simpler.

  Returns 'git' or None.
  """
  if os.path.isdir(os.path.join(root, '.git')):
    return 'git'

  try:
    subprocess2.check_call(
        ['git', 'rev-parse', '--show-cdup'],
        stdout=subprocess2.DEVNULL,
        stderr=subprocess2.DEVNULL,
        cwd=root)
    return 'git'
  except (OSError, subprocess2.CalledProcessError):
    return None


def only_int(val):
  return int(val) if val.isdigit() else 0


class GIT(object):
  current_version = None

  @staticmethod
  def ApplyEnvVars(kwargs):
    env = kwargs.pop('env', None) or os.environ.copy()
    # Don't prompt for passwords; just fail quickly and noisily.
    # By default, git will use an interactive terminal prompt when a username/
    # password is needed.  That shouldn't happen in the chromium workflow,
    # and if it does, then gclient may hide the prompt in the midst of a flood
    # of terminal spew.  The only indication that something has gone wrong
    # will be when gclient hangs unresponsively.  Instead, we disable the
    # password prompt and simply allow git to fail noisily.  The error
    # message produced by git will be copied to gclient's output.
    env.setdefault('GIT_ASKPASS', 'true')
    env.setdefault('SSH_ASKPASS', 'true')
    # 'cat' is a magical git string that disables pagers on all platforms.
    env.setdefault('GIT_PAGER', 'cat')
    return env

  @staticmethod
  def Capture(args, cwd=None, strip_out=True, **kwargs):
    env = GIT.ApplyEnvVars(kwargs)
    output = subprocess2.check_output(
        ['git'] + args, cwd=cwd, stderr=subprocess2.PIPE, env=env, **kwargs)
    output = output.decode('utf-8', 'replace')
    return output.strip() if strip_out else output

  @staticmethod
  def CaptureStatus(cwd, upstream_branch):
    """Returns git status.

    Returns an array of (status, file) tuples."""
    if upstream_branch is None:
      upstream_branch = GIT.GetUpstreamBranch(cwd)
    if upstream_branch is None:
      raise gclient_utils.Error('Cannot determine upstream branch')
    command = [
        '-c',
        'core.quotePath=false',
        'diff',
        '--name-status',
        '--no-renames',
        '-r',
        f'{upstream_branch}...',
    ]
    status = GIT.Capture(command, cwd)
    results = []
    if status:
      for statusline in status.splitlines():
        if m := re.match(r'^(\w)+\t(.+)$', statusline):
                # Only grab the first letter.
          results.append((f'{m[1][0]}      ', m[2]))
        else:
          raise gclient_utils.Error(f'status currently unsupported: {statusline}')
    return results

  @staticmethod
  def GetConfig(cwd, key, default=None):
    try:
      return GIT.Capture(['config', key], cwd=cwd)
    except subprocess2.CalledProcessError:
      return default

  @staticmethod
  def GetBranchConfig(cwd, branch, key, default=None):
    assert branch, 'A branch must be given'
    key = f'branch.{branch}.{key}'
    return GIT.GetConfig(cwd, key, default)

  @staticmethod
  def SetConfig(cwd, key, value=None):
    args = ['config', '--unset', key] if value is None else ['config', key, value]
    GIT.Capture(args, cwd=cwd)

  @staticmethod
  def SetBranchConfig(cwd, branch, key, value=None):
    assert branch, 'A branch must be given'
    key = f'branch.{branch}.{key}'
    GIT.SetConfig(cwd, key, value)

  @staticmethod
  def IsWorkTreeDirty(cwd):
    return GIT.Capture(['status', '-s'], cwd=cwd) != ''

  @staticmethod
  def GetEmail(cwd):
    """Retrieves the user email address if known."""
    return GIT.GetConfig(cwd, 'user.email', '')

  @staticmethod
  def ShortBranchName(branch):
    """Converts a name like 'refs/heads/foo' to just 'foo'."""
    return branch.replace('refs/heads/', '')

  @staticmethod
  def GetBranchRef(cwd):
    """Returns the full branch reference, e.g. 'refs/heads/main'."""
    try:
      return GIT.Capture(['symbolic-ref', 'HEAD'], cwd=cwd)
    except subprocess2.CalledProcessError:
      return None

  @staticmethod
  def GetRemoteHeadRef(cwd, url, remote):
    """Returns the full default remote branch reference, e.g.
    'refs/remotes/origin/main'."""
    if os.path.exists(cwd):
      try:
        # Try using local git copy first
        ref = f'refs/remotes/{remote}/HEAD'
        ref = GIT.Capture(['symbolic-ref', ref], cwd=cwd)
        if not ref.endswith('master'):
          return ref
        # Check if there are changes in the default branch for this particular
        # repository.
        GIT.Capture(['remote', 'set-head', '-a', remote], cwd=cwd)
        return GIT.Capture(['symbolic-ref', ref], cwd=cwd)
      except subprocess2.CalledProcessError:
        pass

    try:
      # Fetch information from git server
      resp = GIT.Capture(['ls-remote', '--symref', url, 'HEAD'])
      regex = r'^ref: (.*)\tHEAD$'
      for line in resp.split('\n'):
        if m := re.match(regex, line):
          return ''.join(GIT.RefToRemoteRef(m[1], remote))
    except subprocess2.CalledProcessError:
      pass
    # Return default branch
    return f'refs/remotes/{remote}/main'

  @staticmethod
  def GetBranch(cwd):
    """Returns the short branch name, e.g. 'main'."""
    if branchref := GIT.GetBranchRef(cwd):
      return GIT.ShortBranchName(branchref)
    return None

  @staticmethod
  def GetRemoteBranches(cwd):
    return GIT.Capture(['branch', '-r'], cwd=cwd).split()

  @staticmethod
  def FetchUpstreamTuple(cwd, branch=None):
    """Returns a tuple containing remote and remote ref,
       e.g. 'origin', 'refs/heads/main'
    """
    try:
      branch = branch or GIT.GetBranch(cwd)
    except subprocess2.CalledProcessError:
      pass
    if branch:
      if upstream_branch := GIT.GetBranchConfig(cwd, branch, 'merge'):
        remote = GIT.GetBranchConfig(cwd, branch, 'remote', '.')
        return remote, upstream_branch

    if upstream_branch := GIT.GetConfig(cwd, 'rietveld.upstream-branch'):
      remote = GIT.GetConfig(cwd, 'rietveld.upstream-remote', '.')
      return remote, upstream_branch

    # Else, try to guess the origin remote.
    remote_branches = GIT.GetRemoteBranches(cwd)
    if 'origin/main' in remote_branches:
      # Fall back on origin/main if it exits.
      return 'origin', 'refs/heads/main'

    if 'origin/master' in remote_branches:
      # Fall back on origin/master if it exits.
      return 'origin', 'refs/heads/master'

    return None, None

  @staticmethod
  def RefToRemoteRef(ref, remote):
    """Convert a checkout ref to the equivalent remote ref.

    Returns:
      A tuple of the remote ref's (common prefix, unique suffix), or None if it
      doesn't appear to refer to a remote ref (e.g. it's a commit hash).
    """
    if m := re.match('^(refs/(remotes/)?)?branch-heads/', ref or ''):
      return 'refs/remotes/branch-heads/', ref.replace(m[0], '')

    if m := re.match(f'^((refs/)?remotes/)?{remote}/|(refs/)?heads/', ref or ''):
      return f'refs/remotes/{remote}/', ref.replace(m[0], '')

    return None

  @staticmethod
  def RemoteRefToRef(ref, remote):
    assert remote, 'A remote must be given'
    if not ref or not ref.startswith('refs/'):
      return None
    if not ref.startswith('refs/remotes/'):
      return ref
    if ref.startswith('refs/remotes/branch-heads/'):
      return 'refs' + ref[len('refs/remotes'):]
    if ref.startswith(f'refs/remotes/{remote}/'):
      return f"refs/heads{ref[len(f'refs/remotes/{remote}'):]}"
    return None

  @staticmethod
  def GetUpstreamBranch(cwd):
    """Gets the current branch's upstream branch."""
    remote, upstream_branch = GIT.FetchUpstreamTuple(cwd)
    if remote != '.' and upstream_branch:
      if remote_ref := GIT.RefToRemoteRef(upstream_branch, remote):
        upstream_branch = ''.join(remote_ref)
    return upstream_branch

  @staticmethod
  def IsAncestor(cwd, maybe_ancestor, ref):
    """Verifies if |maybe_ancestor| is an ancestor of |ref|."""
    try:
      GIT.Capture(['merge-base', '--is-ancestor', maybe_ancestor, ref], cwd=cwd)
      return True
    except subprocess2.CalledProcessError:
      return False

  @staticmethod
  def GetOldContents(cwd, filename, branch=None):
    if not branch:
      branch = GIT.GetUpstreamBranch(cwd)
    if platform.system() == 'Windows':
      # git show <sha>:<path> wants a posix path.
      filename = filename.replace('\\', '/')
    command = ['show', f'{branch}:{filename}']
    try:
      return GIT.Capture(command, cwd=cwd, strip_out=False)
    except subprocess2.CalledProcessError:
      return ''

  @staticmethod
  def GenerateDiff(cwd, branch=None, branch_head='HEAD', full_move=False,
                   files=None):
    """Diffs against the upstream branch or optionally another branch.

    full_move means that move or copy operations should completely recreate the
    files, usually in the prospect to apply the patch for a try job."""
    if not branch:
      branch = GIT.GetUpstreamBranch(cwd)
    command = [
        '-c',
        'core.quotePath=false',
        'diff',
        '-p',
        '--no-color',
        '--no-prefix',
        '--no-ext-diff',
        f"{branch}...{branch_head}",
    ]
    if full_move:
      command.append('--no-renames')
    else:
      command.append('-C')
    # TODO(maruel): --binary support.
    if files:
      command.append('--')
      command.extend(files)
    diff = GIT.Capture(command, cwd=cwd, strip_out=False).splitlines(True)
    for i in range(len(diff)):
      # In the case of added files, replace /dev/null with the path to the
      # file being added.
      if diff[i].startswith('--- /dev/null'):
        diff[i] = f'--- {diff[i + 1][4:]}'
    return ''.join(diff)

  @staticmethod
  def GetDifferentFiles(cwd, branch=None, branch_head='HEAD'):
    """Returns the list of modified files between two branches."""
    if not branch:
      branch = GIT.GetUpstreamBranch(cwd)
    command = [
        '-c',
        'core.quotePath=false',
        'diff',
        '--name-only',
        f"{branch}...{branch_head}",
    ]
    return GIT.Capture(command, cwd=cwd).splitlines(False)

  @staticmethod
  def GetAllFiles(cwd):
    """Returns the list of all files under revision control."""
    command = ['-c', 'core.quotePath=false', 'ls-files', '--', '.']
    return GIT.Capture(command, cwd=cwd).splitlines(False)

  @staticmethod
  def GetPatchName(cwd):
    """Constructs a name for this patch."""
    short_sha = GIT.Capture(['rev-parse', '--short=4', 'HEAD'], cwd=cwd)
    return f"{GIT.GetBranch(cwd)}#{short_sha}"

  @staticmethod
  def GetCheckoutRoot(cwd):
    """Returns the top level directory of a git checkout as an absolute path.
    """
    root = GIT.Capture(['rev-parse', '--show-cdup'], cwd=cwd)
    return os.path.abspath(os.path.join(cwd, root))

  @staticmethod
  def GetGitDir(cwd):
    return os.path.abspath(GIT.Capture(['rev-parse', '--git-dir'], cwd=cwd))

  @staticmethod
  def IsInsideWorkTree(cwd):
    try:
      return GIT.Capture(['rev-parse', '--is-inside-work-tree'], cwd=cwd)
    except (OSError, subprocess2.CalledProcessError):
      return False

  @staticmethod
  def IsDirectoryVersioned(cwd, relative_dir):
    """Checks whether the given |relative_dir| is part of cwd's repo."""
    return bool(GIT.Capture(['ls-tree', 'HEAD', relative_dir], cwd=cwd))

  @staticmethod
  def CleanupDir(cwd, relative_dir):
    """Cleans up untracked file inside |relative_dir|."""
    return bool(GIT.Capture(['clean', '-df', relative_dir], cwd=cwd))

  @staticmethod
  def ResolveCommit(cwd, rev):
    # We do this instead of rev-parse --verify rev^{commit}, since on Windows
    # git can be either an executable or batch script, each of which requires
    # escaping the caret (^) a different way.
    if gclient_utils.IsFullGitSha(rev):
      # git-rev parse --verify FULL_GIT_SHA always succeeds, even if we don't
      # have FULL_GIT_SHA locally. Removing the last character forces git to
      # check if FULL_GIT_SHA refers to an object in the local database.
      rev = rev[:-1]
    try:
      return GIT.Capture(['rev-parse', '--quiet', '--verify', rev], cwd=cwd)
    except subprocess2.CalledProcessError:
      return None

  @staticmethod
  def IsValidRevision(cwd, rev, sha_only=False):
    """Verifies the revision is a proper git revision.

    sha_only: Fail unless rev is a sha hash.
    """
    sha = GIT.ResolveCommit(cwd, rev)
    if sha is None:
      return False
    return sha == rev.lower() if sha_only else True

  @classmethod
  def AssertVersion(cls, min_version):
    """Asserts git's version is at least min_version."""
    if cls.current_version is None:
      current_version = cls.Capture(['--version'], '.')
      matched = re.search(r'git version (.+)', current_version)
      cls.current_version = distutils.version.LooseVersion(matched[1])
    min_version = distutils.version.LooseVersion(min_version)
    return (min_version <= cls.current_version, cls.current_version)
