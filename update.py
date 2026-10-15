import os
import shutil
import subprocess
import sys
import time
from os import path as opath, getenv
from dotenv import load_dotenv

# Try importing logger; fallback to standard print if not ready
try:
    from Thunder.utils.logger import logger
    log_info = logger.info
    log_error = logger.error
    log_warning = logger.warning
except Exception:
    log_info = print
    log_error = print
    log_warning = print

load_dotenv('config.env', override=True)

raw_repo = getenv('UPSTREAM_REPO', '').strip().strip('"').strip("'")
raw_branch = getenv('UPSTREAM_BRANCH', 'main').strip().strip('"').strip("'") or 'main'

UPSTREAM_REPO = raw_repo
UPSTREAM_BRANCH = raw_branch


def run_cmd(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def _write_sync_marker() -> None:
    """Record that this boot already attempted an upstream sync.

    The marker is what stops the auto-update hook in Thunder/__main__.py from
    re-execing into another sync attempt during the same boot - so it must be
    written on failure too, otherwise a failed sync turns into an endless
    re-exec loop instead of a bot that simply boots the code already on disk.
    """
    try:
        with open('.upstream_synced', 'w') as f:
            f.write(str(time.time()))
    except Exception:
        pass


def update_via_archive() -> int:
    import io
    import tarfile
    import urllib.request

    if not UPSTREAM_REPO:
        return 0

    clean_repo = UPSTREAM_REPO.rstrip('/')
    if clean_repo.endswith('.git'):
        clean_repo = clean_repo[:-4]

    archive_url = f"{clean_repo}/archive/refs/heads/{UPSTREAM_BRANCH}.tar.gz"
    log_info(f'Fetching latest code via GitHub archive: {archive_url}...')

    config_backup = '.config.env.bak'
    has_config = opath.exists('config.env')

    try:
        if has_config:
            shutil.copy2('config.env', config_backup)

        req = urllib.request.Request(
            archive_url,
            headers={'User-Agent': 'CloudStation-Updater/2.1.0'}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                log_error(f'Failed to download upstream archive: HTTP {resp.status}')
                return 1
            archive_bytes = resp.read()

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as tar:
            members = tar.getmembers()
            if not members:
                log_error('Empty archive received from upstream repository.')
                return 1

            top_dir = members[0].name.split('/')[0]
            for member in members:
                parts = member.name.split('/')
                if len(parts) <= 1 or parts[0] != top_dir:
                    continue
                rel_path = '/'.join(parts[1:])
                if not rel_path or rel_path == 'config.env':
                    continue

                target_path = opath.abspath(rel_path)
                if member.isdir():
                    os.makedirs(target_path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(opath.dirname(target_path), exist_ok=True)
                    with tar.extractfile(member) as src_f, open(target_path, 'wb') as dst_f:
                        shutil.copyfileobj(src_f, dst_f)

        log_info(f'Successfully updated and synchronized with upstream archive (branch: {UPSTREAM_BRANCH})')
        os.environ['UPSTREAM_SYNCED'] = '1'
        _write_sync_marker()
        return 0
    except Exception as e:
        log_error(f'Archive fallback update failed: {e}')
        return 1
    finally:
        if has_config and opath.exists(config_backup):
            try:
                shutil.copy2(config_backup, 'config.env')
                os.remove(config_backup)
            except Exception as e:
                log_warning(f'Could not restore config.env backup: {e}')


def update_codebase() -> int:
    if not UPSTREAM_REPO:
        log_info('No UPSTREAM_REPO configured. Booting from local codebase.')
        return 0

    marker_path = '.upstream_synced'
    if opath.exists(marker_path):
        try:
            with open(marker_path, 'r') as f:
                last_sync = float(f.read().strip())
            if (time.time() - last_sync) < 60:
                log_info('Repository already synchronized recently. Skipping redundant update.')
                return 0
        except Exception:
            pass

    log_info(f'Connecting to UPSTREAM_REPO: {UPSTREAM_REPO} (branch: {UPSTREAM_BRANCH})...')

    git_check = run_cmd('git --version')
    if git_check.returncode != 0:
        log_info('Git binary not available in PATH. Using direct archive sync fallback...')
        return update_via_archive()

    config_backup = '.config.env.bak'
    has_config = opath.exists('config.env')

    try:
        if has_config:
            shutil.copy2('config.env', config_backup)

        is_git_repo = opath.exists('.git') and run_cmd('git rev-parse --is-inside-work-tree').returncode == 0

        if not is_git_repo:
            log_info('Initializing fresh git repository for upstream sync...')
            if opath.exists('.git'):
                try:
                    def remove_readonly(func, path, excinfo):
                        import stat
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    shutil.rmtree('.git', onerror=remove_readonly)
                except Exception as e:
                    log_warning(f'Could not remove stale .git: {e}')

            init_cmds = (
                f'git init -q && '
                f'git config user.email cloudstation@update.local && '
                f'git config user.name CloudStation && '
                f'git remote add origin {UPSTREAM_REPO}'
            )
            r = run_cmd(init_cmds)
            if r.returncode != 0:
                log_error(f'Failed to initialize git repository: {r.stderr.strip()}')
                return 1
        else:
            remotes = run_cmd('git remote').stdout.split()
            if 'origin' in remotes:
                run_cmd(f'git remote set-url origin {UPSTREAM_REPO}')
            else:
                run_cmd(f'git remote add origin {UPSTREAM_REPO}')

        log_info(f'Fetching latest code from {UPSTREAM_BRANCH}...')
        fetch_cmd = f'git fetch origin {UPSTREAM_BRANCH} --depth=1 -q'
        fetch_res = run_cmd(fetch_cmd)
        if fetch_res.returncode != 0:
            log_error(
                f'Failed to fetch from UPSTREAM_REPO ({UPSTREAM_REPO}) at branch ({UPSTREAM_BRANCH}): {fetch_res.stderr.strip()}'
            )
            return 1

        # `-f` (force) is required, not cosmetic: on a freshly deployed tree
        # (Heroku slug, Docker image, plain tar extract) the application files
        # exist on disk but are untracked, so a plain `git checkout -B` aborts
        # with "untracked working tree files would be overwritten by checkout"
        # and the bot then never boots. -f overwrites only files that the
        # upstream commit also contains, so untracked config.env is preserved.
        reset_cmd = (
            f'git checkout -f -B {UPSTREAM_BRANCH} origin/{UPSTREAM_BRANCH} -q && '
            f'git reset --hard origin/{UPSTREAM_BRANCH} -q'
        )
        reset_res = run_cmd(reset_cmd)
        if reset_res.returncode == 0:
            log_info(f'Successfully updated and synchronized with {UPSTREAM_REPO} (branch: {UPSTREAM_BRANCH})')
            os.environ['UPSTREAM_SYNCED'] = '1'
            _write_sync_marker()
            return 0
        else:
            log_error(f'Failed to reset to upstream branch: {reset_res.stderr.strip()}')
            log_info('Retrying upstream sync via direct GitHub archive fallback...')
            return update_via_archive()

    except Exception as e:
        log_error(f'Unexpected exception during upstream update: {e}')
        return 1

    finally:
        if has_config and opath.exists(config_backup):
            try:
                shutil.copy2(config_backup, 'config.env')
                os.remove(config_backup)
            except Exception as e:
                log_warning(f'Could not restore config.env backup: {e}')


if __name__ == '__main__':
    exit_code = update_codebase()
    if exit_code != 0:
        # A failed sync must never stop the bot from booting: the Procfile runs
        # `python update.py && python -m Thunder`, so a non-zero exit here leaves
        # the dyno with nothing running at all. Boot the code already on disk
        # and mark this boot as handled so the auto-update hook in
        # Thunder/__main__.py does not re-exec into another sync attempt.
        log_error('Upstream sync failed - booting the code currently on disk instead.')
        _write_sync_marker()
        exit_code = 0
    sys.exit(exit_code)
