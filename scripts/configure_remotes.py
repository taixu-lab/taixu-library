"""Configure one Git checkout to push the same commits to GitHub and Gitee."""
import subprocess

GITHUB = 'https://github.com/wkbin/taixu-library.git'
GITEE = 'https://gitee.com/wang_ke_bin/taixu-library.git'


def git(*args, check=True):
    return subprocess.run(['git', *args], check=check, capture_output=True, text=True)


def configure():
    if git('remote', 'get-url', 'origin').stdout.strip() != GITHUB:
        raise ValueError('Unexpected origin URL; refusing to change unrelated repository')
    mirror = git('remote', 'get-url', 'gitee', check=False)
    if mirror.returncode:
        git('remote', 'add', 'gitee', GITEE)
    elif mirror.stdout.strip() != GITEE:
        raise ValueError('Existing gitee remote points elsewhere')
    urls = git('config', '--get-all', 'remote.origin.pushurl', check=False).stdout.splitlines()
    if any(u not in (GITHUB, GITEE) for u in urls):
        raise ValueError('Unexpected existing push URL; refusing to overwrite')
    for url in (GITHUB, GITEE):
        if url not in urls: git('remote', 'set-url', '--add', '--push', 'origin', url)
    print('Configured dual push. Commit once, then git push origin main sends the same commits to both hosts.')
    print('Pushes are not atomic across hosts; check both results and retry a failed host without force.')


if __name__ == '__main__': configure()
