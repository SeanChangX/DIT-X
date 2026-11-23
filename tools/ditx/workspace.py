"""What the workspace contains: repos, domains, robots, settings.

Every reader here is forgiving (missing file -> empty result) because the TUI
and Tab completion call it on every keystroke and must never crash.
"""

import configparser
from dataclasses import dataclass, field
import os
import platform
import re
import subprocess

REPOS_FILE = 'ditx.repos'
ROBOTS_FILE = os.path.join('config', 'robots.ini')


def find_root(start=None):
    """Walk up from `start` (like git/colcon) to the dir that holds ditx.repos."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, REPOS_FILE)) and os.path.isdir(
            os.path.join(d, 'tools', 'ditx')
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def parse_env_file(path):
    out = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def parse_repos(path):
    """Minimal reader for the vcstool .repos format (the subset vcs export writes)."""
    repos, cur = {}, None
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return repos
    for raw in lines:
        line = raw.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(':')
        val = val.strip().strip('"').strip("'")
        if indent == 2 and not val:
            cur = repos.setdefault(key, {})
        elif indent >= 4 and cur is not None:
            cur[key] = val
    return repos


def native_arch():
    m = platform.machine().lower()
    return {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(m, m)


def mem_total_gb():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 8.0


def auto_jobs(mem_gb=None, cpus=None):
    """(parallel_workers, make_jobs, docker_memory_gb) that will not OOM the machine.

    colcon with no arguments builds many packages at once and every package runs
    make -j<cpus>: workers x jobs compilers, ~1.5 GB each for ROS C++. When RAM
    runs out, systemd-oomd kills the biggest *low-priority* cgroup, which tends
    to be the desktop session, not the build. So: budget the compilers by RAM,
    and cap the build container at 80% so the OOM killer stays inside it.
    """
    mem_gb = mem_total_gb() if mem_gb is None else mem_gb
    cpus = (os.cpu_count() or 2) if cpus is None else cpus
    total = max(1, min(cpus, int(mem_gb * 0.75 / 1.5)))
    workers = max(1, min(4, total // 4))
    return workers, max(1, total // workers), max(2, int(mem_gb * 0.8))


@dataclass
class Robot:
    name: str
    host: str = 'local'
    arch: str = 'amd64'
    domain_id: str = '0'
    profiles: list = field(default_factory=list)
    tag: str = 'stable'
    discovery: str = 'subnet'  # subnet | localhost (keep DDS traffic on this machine)
    note: str = ''
    # topics of systems outside DIT-X on this robot (e.g. a web dashboard), fnmatch patterns
    foreign_topics: list = field(default_factory=list)

    @property
    def is_local(self):
        return self.host in ('local', 'localhost', '')


DIT_HOST = re.compile(r'(?i)dit-\d{4}-\d{1,3}(\.local)?')  # DIT-Scripts hostnames


class Workspace:
    def __init__(self, root):
        self.root = root
        self.env = {
            **parse_env_file(os.path.join(root, '.env.example')),
            **parse_env_file(os.path.join(root, '.env')),
        }

    # settings: environment > .env > .env.example > default
    def setting(self, key, default=''):
        return os.environ.get(key) or self.env.get(key) or default

    @property
    def distro(self):
        return self.setting('ROS_DISTRO', 'humble')

    @property
    def robot_user(self):
        """Your login on DIT-Scripts robots: your team's account (ditx init)."""
        return self.setting('DIT_ROBOT_USER', 'main')

    @property
    def registry(self):
        return self.setting('REGISTRY', 'ghcr.io/seanchangx')

    def dev_image(self):
        return (
            self.setting('DIT_DEV_IMAGE')
            or f'{self.registry}/eurobot-dev:{self.setting("DIT_DEV_TAG", "develop")}'
        )

    def cross_image(self):
        """dev image + arm64 sysroot: builds for arm64 on an amd64 machine (docker/cross)."""
        return (
            self.setting('DIT_CROSS_IMAGE')
            or f'{self.registry}/eurobot-cross:{self.setting("DIT_DEV_TAG", "develop")}'
        )

    @property
    def repos_file(self):
        return self.setting('DIT_REPOS_FILE') or os.path.join(self.root, REPOS_FILE)

    def repos(self):
        return parse_repos(self.repos_file)

    def domains(self):
        """Domain names declared in the repos file (path domains/<name>)."""
        return [p.split('/', 1)[1] for p in self.repos() if p.startswith('domains/')]

    def domain_path(self, name):
        return os.path.join(self.root, 'domains', name)

    def synced_domains(self):
        return [d for d in self.domains() if os.path.isdir(self.domain_path(d))]

    def packages(self, domains=None):
        """ROS package names found under the given domains (by package.xml)."""
        out = []
        for d in domains or self.synced_domains():
            for dirpath, dirnames, filenames in os.walk(self.domain_path(d)):
                dirnames[:] = [x for x in dirnames if not x.startswith('.')]
                if 'COLCON_IGNORE' in filenames:
                    dirnames[:] = []
                    continue
                if 'package.xml' in filenames:
                    with open(os.path.join(dirpath, 'package.xml'), encoding='utf-8') as f:
                        m = re.search(r'<name>\s*([^<\s]+)\s*</name>', f.read())
                    if m:
                        out.append(m.group(1))
                    dirnames[:] = []
        return sorted(out)

    def robots(self):
        cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
        cp.read(os.path.join(self.root, ROBOTS_FILE), encoding='utf-8')
        out = {}
        for name in cp.sections():
            s = cp[name]
            host = s.get('host', 'local')
            # DIT-Scripts robots have one login per team: everyone connects with their own
            # (DIT_ROBOT_USER); other hosts are left to ~/.ssh/config
            if '@' not in host and DIT_HOST.fullmatch(host):
                host = f'{self.robot_user}@{host}'
            out[name] = Robot(
                name=name,
                host=host,
                arch=s.get('arch', 'amd64'),
                domain_id=s.get('domain_id', '0'),
                profiles=[p.strip() for p in s.get('profiles', '').split(',') if p.strip()],
                tag=s.get('tag', 'stable'),
                discovery=s.get('discovery', 'subnet'),
                note=s.get('note', ''),
                foreign_topics=s.get('foreign_topics', '').replace(',', ' ').split(),
            )
        return out

    def robots_path(self):
        return os.path.join(self.root, ROBOTS_FILE)

    def add_robot(self, name, fields):
        """Append a [name] section to robots.ini; everything already there (comments too) stays."""
        path = self.robots_path()
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = ''
        block = [f'[{name}]'] + [f'{k} = {v}' for k, v in fields.items() if v not in (None, '')]
        text = text.rstrip('\n') + ('\n\n' if text.strip() else '') + '\n'.join(block) + '\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def remove_robot(self, name):
        """Drop the [name] section (and the blank lines right before it); keep everything else."""
        path = self.robots_path()
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
        out, skipping = [], False
        for line in lines:
            m = re.match(r'^\s*\[([^\]]+)\]\s*$', line)
            if m:
                skipping = m.group(1).strip() == name
                if skipping:
                    while out and not out[-1].strip():
                        out.pop()
                    continue
            # inside the removed section only its keys go; comments may belong to what follows
            if not skipping or line.lstrip().startswith(('#', ';')):
                out.append(line)
        text = '\n'.join(out).rstrip('\n') + '\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def default_robot(self):
        return self.setting('DIT_ROBOT', 'sim')

    def build_tag(self, arch):
        return f'{self.distro}-{arch}'

    def build_dir(self, arch, name):
        """build/<distro>-<arch>/<base|domain>/{build,install,log}: one install per layer."""
        return os.path.join(self.root, 'build', self.build_tag(arch), name)

    def git(self, path, *args, check=False):
        r = subprocess.run(['git', '-C', path, *args], capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip() if r.returncode == 0 else ''

    def plugins(self):
        """git/kubectl-style plugins: executables named ditx-<name> in tools/ext or on PATH.

        Returns {name: (path, summary)}; summary is the `# ditx-plugin: ...` line of the file.
        """
        found = {}
        dirs = [os.path.join(self.root, 'tools', 'ext')]
        dirs += os.environ.get('PATH', '').split(os.pathsep)
        for d in dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for f in names:
                path = os.path.join(d, f)
                if not f.startswith('ditx-') or not os.access(path, os.X_OK) or os.path.isdir(path):
                    continue
                name = f[len('ditx-') :]
                if name in found:
                    continue  # tools/ext wins over PATH
                summary = ''
                try:
                    with open(path, encoding='utf-8', errors='replace') as fh:
                        for line in fh.readlines()[:10]:
                            if 'ditx-plugin:' in line:
                                summary = line.split('ditx-plugin:', 1)[1].strip()
                                break
                except OSError:
                    pass
                found[name] = (path, summary)
        return found
