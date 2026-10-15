import os
import shutil
import subprocess
import sys
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


def update_codebase() -> int:
    if not UPSTREAM_REPO:
        log_info('No UPSTREAM_REPO configured. Booting from local codebase.')
        return 0

    log_info(f'Connecting to UPSTREAM_REPO: {UPSTREAM_REPO} (branch: {UPSTREAM_BRANCH})...')

    git_check = run_cmd('git --version')
    if git_check.returncode != 0:
        log_warning('Git is not installed or not available in PATH. Skipping upstream update.')
        return 1

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

        reset_cmd = (
            f'git checkout -B {UPSTREAM_BRANCH} origin/{UPSTREAM_BRANCH} -q && '
            f'git reset --hard origin/{UPSTREAM_BRANCH} -q'
        )
        reset_res = run_cmd(reset_cmd)
        if reset_res.returncode == 0:
            log_info(f'Successfully updated and synchronized with {UPSTREAM_REPO} (branch: {UPSTREAM_BRANCH})')
            os.environ['UPSTREAM_SYNCED'] = '1'
            return 0
        else:
            log_error(f'Failed to reset to upstream branch: {reset_res.stderr.strip()}')
            return 1

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
    sys.exit(exit_code)
