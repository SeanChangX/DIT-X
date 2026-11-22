"""ditx command line: argparse from commands.py + one cmd_<name> handler each.

Exit codes: 0 ok, 1 failed, 2 usage error (argparse), 130 interrupted.
"""

import argparse
import concurrent.futures
import difflib
import getpass
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

from . import __version__, ui
from .commands import BY_NAME, COMMANDS, GROUPS
from .workspace import Workspace, auto_jobs, find_root, native_arch


class Fail(Exception):
    """A handled failure: message for the user, exit code 1."""


# ════════════════════════════════════════════════════════════════════════════
# argparse from the command table
# ════════════════════════════════════════════════════════════════════════════


GLOBAL_FLAGS = [
    # (short, long, dest, help) -- accepted before or after the command
    ('-n', '--dry-run', 'dry_run', 'print what would run, change nothing'),
    ('-v', '--verbose', 'verbose', 'show the full output of every tool'),
    ('-q', '--quiet', 'quiet', 'print only warnings and errors'),
]
INFO_FLAGS = [('-h', '--help', 'show help'), ('-V', '--version', 'print the version')]


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """`-d DOMAIN...` instead of argparse's `-d DOMAIN [DOMAIN ...]`."""

    def _format_args(self, action, default_metavar):
        if action.nargs in ('+', '*'):
            (metavar,) = self._metavar_formatter(action, default_metavar)(1)
            return f'{metavar}...'
        return super()._format_args(action, default_metavar)


class DitxParser(argparse.ArgumentParser):
    """argparse errors in the same shape as every other ditx error, with a hint."""

    def error(self, message):
        m = re.match(r'unrecognized arguments: (\S+)', message)
        if m:
            bad = m.group(1)
            flags = [f for a in self._actions for f in a.option_strings]
            near = difflib.get_close_matches(bad.split('=')[0], flags, n=1, cutoff=0.6)
            message = f'unknown flag {bad}' if bad.startswith('-') else f'unexpected argument {bad}'
            if near:
                message += f'; did you mean {near[0]}?'
        ui.error(f'{self.prog}: {message}')
        ui.info(f'see: {self.prog} --help')
        sys.exit(2)


def command_parser(c):
    """The parser of one command (what `ditx <command>` accepts)."""
    positionals = ' '.join(
        f'[{o.dest.upper()}]' if o.default is not None or o.kind == 'multi' else o.dest.upper()
        for o in c.opts
        if not o.flags
    )
    epilog = []
    choices = [o for o in c.opts if o.choice_help]
    if choices:
        epilog.append('Choices:')
        pairs = [
            (f'{o.flags[-1] if o.flags else o.dest} {v}', h)
            for o in choices
            for v, h in o.choice_help.items()
        ]
        w = max(len(p) for p, _ in pairs) + 2
        epilog += [f'  {p:<{w}}{h}' for p, h in pairs]
        epilog.append('')
    if c.examples:
        epilog += ['Examples:'] + [f'  {e}' for e in c.examples] + ['']
    if c.confirm:
        epilog += [f'Asks for confirmation ({c.confirm}); -y skips it.', '']
    sp = DitxParser(
        prog=f'ditx {c.name}',
        usage=f'ditx {c.name} {positionals + " " if positionals else ""}[flags]',
        description=c.help,
        epilog='\n'.join(epilog).rstrip(),
        formatter_class=HelpFormatter,
    )
    for short, long, dest, help_ in GLOBAL_FLAGS:
        sp.add_argument(short, long, dest=dest, action='store_true', help=help_)
    for o in c.opts:
        kw = {'help': o.help}
        if o.kind == 'flag':
            kw['action'] = 'store_true'
        elif o.kind == 'multi':
            kw['nargs'] = '*' if not o.flags else '+'
            kw['default'] = o.default or []
        else:
            kw['default'] = o.default
            if o.flags == () and o.default is not None:
                kw['nargs'] = '?'
        if isinstance(o.values, tuple):
            kw['choices'] = o.values
        if o.metavar and o.flags:
            kw['metavar'] = o.metavar
        if o.flags:
            sp.add_argument(*o.flags, dest=o.dest, **kw)
        else:
            sp.add_argument(o.dest, **kw)
    return sp


def build_parser():
    """One parser for everything (tests, tools): `ditx <command> ...`."""
    p = DitxParser(prog='ditx', add_help=False)
    for short, long, dest, help_ in GLOBAL_FLAGS:
        p.add_argument(short, long, dest=dest, action='store_true', help=help_)
    sub = p.add_subparsers(dest='command', metavar='<command>')
    for c in COMMANDS:
        sub.add_parser(
            c.name, parents=[command_parser(c)], add_help=False, conflict_handler='resolve'
        )
    return p


def top_help(ws):
    """gh-style overview: grouped commands, plugins, global flags, examples."""

    def head(text):
        return ui.paint(text, None if not ui.COLOR_OUT else ui.WHITE, bold=True, stream=sys.stdout)

    out = [
        'ditx (DIT-X): build, test and deploy the Eurobot workspace.',
        'Run `ditx` alone for the interactive command builder.',
        '',
        head('Usage'),
        '  ditx <command> [flags]',
        '',
    ]
    for g, title in GROUPS:
        out.append(head(f'{title} commands'))
        out += [f'  {c.name:<11} {c.help}' for c in COMMANDS if c.group == g]
        out.append('')
    plugins = ws.plugins() if ws else {}
    if plugins:
        out.append(
            head('Plugins')
            + ui.paint('  (ditx-<name> in tools/ext or on PATH)', ui.GRAY, stream=sys.stdout)
        )
        out += [f'  {n:<11} {s or "-"}' for n, (_, s) in sorted(plugins.items())]
        out.append('')
    out.append(head('Global flags'))
    out += [f'  {s}, {lng:<11} {h}' for s, lng, _, h in GLOBAL_FLAGS]
    out += [f'  {s}, {lng:<11} {h}' for s, lng, h in INFO_FLAGS]
    out += [
        '',
        head('Examples'),
        '  ditx sync && ditx build',
        '  ditx test --integration',
        '  ditx deploy -d localization -r sim',
        '  ditx robot add white --host DIT-2026-11',
        '',
        'Run `ditx help <command>` for details on a command.',
    ]
    print('\n'.join(out))


def split_globals(argv):
    """Leading global flags (before the command) -> (flags set, rest)."""
    flags, i = set(), 0
    known = {f: dest for s, lng, dest, _ in GLOBAL_FLAGS for f in (s, lng)}
    known.update({f: lng.lstrip('-') for s, lng, _ in INFO_FLAGS for f in (s, lng)})
    while i < len(argv) and argv[i] in known:
        flags.add(known[argv[i]])
        i += 1
    return flags, argv[i:]


def confirmed(c, args):
    """Destructive commands ask first: [y/N] on a terminal, --yes otherwise."""
    if not c.confirm or args.dry_run or getattr(args, 'yes', False):
        return True
    if os.environ.get('DITX_CONFIRMED') == '1':  # the TUI already asked
        return True
    if not sys.stdin.isatty():
        return None  # usage error: needs --yes
    print(
        f'{ui.paint("?", ui.WARN, bold=True)} ditx {c.name} {c.confirm}. Continue? [y/N] ',
        end='',
        file=sys.stderr,
        flush=True,
    )
    try:
        return input().strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def open_tui(ws, flags):
    from .tui import run_tui

    try:
        return run_tui(ws, dry_run='dry_run' in flags, verbose='verbose' in flags)
    except TypeError:  # older TUI without the verbose preset
        return run_tui(ws, dry_run='dry_run' in flags)


def main(argv=None):
    global LOG
    argv = sys.argv[1:] if argv is None else argv
    root = find_root() or os.environ.get('DIT_ROOT')
    if not root:
        ui.error('not inside an Eurobot workspace (no ditx.repos found above this directory)')
        return 1
    ws = Workspace(root)
    if argv and argv[0] == '__complete':
        from .complete import complete

        mode = None
        if argv[1:2] and argv[1].startswith('--describe'):
            mode, argv = argv[1], argv[1:]
        print('\n'.join(complete(ws, argv[1:], describe=mode)))
        return 0
    flags, rest = split_globals(argv)
    if 'version' in flags:
        print(f'ditx {__version__}')
        return 0
    if 'help' in flags or rest[:1] == ['help']:
        name = rest[1] if rest[:1] == ['help'] and len(rest) > 1 else (rest[0] if rest else '')
        if name in BY_NAME:
            command_parser(BY_NAME[name]).print_help()
            return 0
        if name and name in ws.plugins():
            rest = [name, '--help']
        elif name and name != 'help':
            ui.error(f'no command or plugin called {name!r}')
            return 2
        else:
            top_help(ws)
            return 0
    if not rest or rest == ['tui']:
        return open_tui(ws, flags)
    name = rest[0]
    if name not in BY_NAME:
        plugins = ws.plugins()
        if name in plugins:  # `ditx foo ...` runs the ditx-foo plugin (git/kubectl style)
            env = {**os.environ, 'DITX_ROOT': ws.root, 'DIT_ROOT': ws.root}
            for f in ('dry_run', 'verbose', 'quiet'):
                env[f'DITX_{f.upper()}'] = '1' if f in flags else env.get(f'DITX_{f.upper()}', '')
            os.execve(plugins[name][0], [plugins[name][0], *rest[1:]], env)
        if name.startswith('-'):
            ui.error(f'unknown flag {name}')
        else:
            near = difflib.get_close_matches(name, list(BY_NAME) + list(plugins), n=1, cutoff=0.6)
            ui.error(f'unknown command {name!r}' + (f'; did you mean {near[0]!r}?' if near else ''))
        ui.info('see: ditx --help')
        return 2
    c = BY_NAME[name]
    args = command_parser(c).parse_args(rest[1:])
    args.command = name
    for f in ('dry_run', 'verbose', 'quiet'):
        setattr(args, f, getattr(args, f) or f in flags)
    ui.VERBOSE = ui.VERBOSE or args.verbose
    ui.QUIET = args.quiet and not args.verbose
    ui.DRY_RUN = args.dry_run
    LOG = new_log(ws, args.command)
    ui.ROOT = ws.root
    try:
        ok = confirmed(c, args)
        if ok is None:
            ui.error(f'ditx {c.name} {c.confirm}: pass --yes to run it without a terminal')
            return 2
        if not ok:
            ui.info('cancelled, nothing changed')
            return 1
        return globals()['cmd_' + args.command](ws, args) or 0
    except Fail as e:
        ui.error(str(e))
        if not ui.VERBOSE and os.path.isfile(LOG):
            ui.info(f'full log: {os.path.relpath(LOG, ws.root)}   (or rerun with -v)')
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════


LOG = os.devnull  # build/logs/<command>-<time>.log, set by main()


def new_log(ws, command):
    """Path for this run's full tool output; keeps the 30 newest logs."""
    d = os.path.join(ws.root, 'build', 'logs')
    if os.path.isdir(d):
        old = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
        for f in old[:-29]:
            os.remove(f)
    return os.path.join(d, f'{command}-{time.strftime("%Y%m%d-%H%M%S")}.log')


def run(cmd, args=None, check=True, quiet=True, on_line=None, **kw):
    """Run a command (list or shell string); honours --dry-run.

    quiet: output goes to the log file (shown with -v, or its tail on failure);
    otherwise it is attached to the terminal (interactive shells, `logs -f`).
    """
    text = cmd if isinstance(cmd, str) else shlex.join(cmd)
    dry = args is not None and getattr(args, 'dry_run', False)
    ui.command(text if dry or len(text) < 400 else text[:400] + ' …', force=dry)
    if dry:
        return subprocess.CompletedProcess(cmd, 0, '', '')
    if quiet:
        rc, lines = ui.run_logged(cmd, LOG, on_line, **kw)
        r = subprocess.CompletedProcess(cmd, rc, '\n'.join(lines), '')
    else:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
    if check and r.returncode:
        if quiet and not ui.VERBOSE:
            ui.show_tail(r.stdout.splitlines())
        raise Fail(f'{text.split()[0]} failed with exit code {r.returncode}')
    return r


class Sections:
    """Turns `@@begin NAME` / `@@end NAME RC` markers printed by a container script into
    one ui.Task line per section; `@@note TEXT` becomes a warning line.

    status(line) -> str|None   live status while the section runs
    summary(lines) -> str      text after the ✓/✗
    tail(lines) -> lines       what to show when the section failed
    """

    def __init__(self, status=None, summary=None, tail=None):
        self.status_fn, self.summary_fn, self.tail_fn = status, summary, tail
        self.task, self.lines, self.failed, self.passed = None, [], [], []

    def __call__(self, line):
        if line.startswith('@@begin '):
            self.task, self.lines = ui.Task(line[8:].strip()), []
        elif line.startswith('@@end ') and self.task:
            rc = int(line.rsplit(None, 1)[1])
            summary = self.summary_fn(self.lines) if self.summary_fn else ''
            if rc == 0:
                self.task.done(summary)
                self.passed.append(self.task.label)
            else:
                self.task.fail(summary or f'exit code {rc}')
                self.failed.append(self.task.label)
                if not ui.VERBOSE:
                    ui.show_tail(self.tail_fn(self.lines) if self.tail_fn else self.lines)
            self.task = None
        elif line.startswith('@@note '):
            msg = f'{ui.paint("!", ui.WARN, bold=True)} {line[7:]}'
            self.task.print(msg) if self.task else print(msg, file=sys.stderr)
        else:
            self.lines.append(line)
            if self.task and self.status_fn:
                st = self.status_fn(line)
                if st:
                    self.task.status(st)

    def close(self):
        """The script stopped inside a section (crash, Ctrl+C)."""
        if self.task:
            self.task.fail('aborted')
            self.failed.append(self.task.label)
            if not ui.VERBOSE:
                ui.show_tail(self.lines)
            self.task = None


def section_sh(name, body, stop=True):
    """Bash that wraps `body` in @@begin/@@end markers (see Sections)."""
    return f'echo "@@begin {name}"\n( {body}\n)\nrc=$?\necho "@@end {name} $rc"\n' + (
        '[ $rc -eq 0 ] || exit $rc\n' if stop else '[ $rc -eq 0 ] || fail=1\n'
    )


def pick_domains(ws, wanted, synced_only=True):
    known = ws.domains()
    for d in wanted:
        if d not in known:
            raise Fail(
                f'unknown domain {d!r}; known: {", ".join(known) or "(none, check ditx.repos)"}'
            )
    pool = ws.synced_domains() if synced_only else known
    out = [d for d in (wanted or pool) if d in pool]
    missing = [d for d in wanted if d not in pool]
    if missing:
        raise Fail(f'not synced yet: {", ".join(missing)} (run: ditx sync {" ".join(missing)})')
    if not out:
        raise Fail('no domains synced yet (run: ditx sync)')
    return out


def parse_jobs(jobs='auto', workers='auto'):
    """(colcon workers, make -j, container RAM cap GB).

    auto keeps workers x jobs inside the RAM budget (see workspace.auto_jobs); setting
    one of them by hand recomputes the other from the same budget.
    """
    auto_w, auto_j, mem = auto_jobs()
    budget = auto_w * auto_j

    def number(name, v):
        if v in (None, '', 'auto'):
            return None
        if not str(v).isdigit() or int(v) < 1:
            raise Fail(f'{name} must be "auto" or a positive number, got {v!r}')
        return int(v)

    j, w = number('--jobs', jobs), number('--workers', workers)
    if j is None and w is None:
        return auto_w, auto_j, mem
    if w is None:
        w = max(1, budget // j)
    if j is None:
        j = max(1, budget // w)
    return w, j, mem


def ensure_image(image, args, label='dev image', target='dev'):
    if getattr(args, 'dry_run', False):
        return
    if subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode == 0:
        return
    if image.endswith(':local'):  # built on this machine, never in a registry
        raise Fail(
            f'{label} {image} is not built yet  → ditx image --target {target}  '
            f'(once; again after the Dockerfile or dependencies change)'
        )
    with ui.Task(label) as t:
        t.status(f'pulling {image}')
        if run(['docker', 'pull', image], check=False).returncode:
            t.fail('not available')
            raise Fail(
                f'image {image} not available.\n'
                f'  Build it here:  ditx image --target {target}  (with DIT_DEV_TAG=local)\n'
                f'  (or log in to the registry that publishes it: docker login)'
            )
        t.done(f'pulled {image}')


def cross_compiles(arch):
    """True when `arch` is built with the cross image instead of qemu emulation."""
    return native_arch() == 'amd64' and arch == 'arm64'


def dev_run(
    ws, args, script, arch=None, interactive=False, mem_gb=None, on_line=None, plan=None,
    emulate=False,
):  # fmt: skip
    """Run a bash script in the dev container, workspace mounted at the same path.

    Same path inside and outside means compile_commands.json, error messages and
    test reports all point at files you can open on the host. Returns the
    CompletedProcess (stdout = all output lines); the caller decides what failed.

    plan: [(label, what it would do)] printed instead of the raw docker command and
    script in a dry run (-v shows the full command as well).

    A foreign `arch` is cross compiled when possible (arm64 on amd64: the cross image,
    native speed, no qemu); emulate=True (or any other pair) runs the foreign image
    under qemu instead, which is what executing arm64 programs needs.
    """
    arch = arch or native_arch()
    cross = cross_compiles(arch) and not emulate
    image, label = (ws.cross_image(), 'cross image') if cross else (ws.dev_image(), 'dev image')
    if getattr(args, 'dry_run', False) and plan is not None:
        ui.planned(label, image + (f' · RAM cap {mem_gb} GB' if mem_gb else ''))
        for label, what in plan:
            ui.planned(label, what)
        if not ui.VERBOSE:
            return subprocess.CompletedProcess([], 0, '', '')
    ensure_image(image, args, label, 'cross' if cross else 'dev')
    cmd = ['docker', 'run', '--rm', '--init', '--network', 'host', '--ipc', 'host']
    cmd += ['--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp/dit-home']
    cmd += ['-e', f'DIT_ROOT={ws.root}', '-e', 'CCACHE_DIR=/ccache', '-e', 'PYTHONUNBUFFERED=1']
    cmd += ['-e', 'PYTHONDONTWRITEBYTECODE=1']  # no __pycache__ in the source tree
    cmd += ['-v', f'{ws.root}:{ws.root}', '-w', ws.root, '-v', 'eurobot-ccache:/ccache']
    if mem_gb:
        cmd += ['--memory', f'{mem_gb}g', '--memory-swap', f'{mem_gb}g']
    if arch != native_arch() and not cross:
        cmd += ['--platform', f'linux/{arch}']
    if interactive:
        cmd += ['-it', '-e', 'TERM', '-e', 'COLORTERM']
    cmd += [image, 'bash', '-c', 'mkdir -p "$HOME"; ' + script]
    return run(cmd, args, check=False, quiet=not interactive, on_line=on_line)


def source_base_sh(ws, arch):
    """Shell snippet: source the local base build if there is one, else the image's."""
    local = ws.build_dir(arch, 'base')
    return (
        f'if [ -f {local}/install/setup.bash ]; then\n'
        f'  source {local}/install/setup.bash; BASE_FP=$(cat {local}/FINGERPRINT)\n'
        f'  echo "@@note using a LOCAL base build ($BASE_FP); the image base is '
        f'$(cat /opt/eurobot/base/FINGERPRINT). Remove it with: ditx clean --base"\n'
        f'else\n'
        f'  BASE_FP=$(cat /opt/eurobot/base/FINGERPRINT)\n'
        f'fi\n'
    )


def domain_packages(ws, domain, packages):
    """Subset of `packages` that live in `domain` (None = no filter)."""
    if not packages:
        return None
    return [p for p in packages if p in ws.packages([domain])]


# ════════════════════════════════════════════════════════════════════════════
# develop
# ════════════════════════════════════════════════════════════════════════════


def colcon_build_sh(src, bdir, workers, build_type, packages=None, testing=True, extra=''):
    sel = f' --packages-up-to {" ".join(packages)}' if packages else ''
    return (
        f'colcon --log-base {bdir}/log build --base-paths {src} '
        f'--build-base {bdir}/build --install-base {bdir}/install '
        f'--merge-install --parallel-workers {workers}{sel} {extra}'
        f' --cmake-args -DCMAKE_BUILD_TYPE={build_type} -DCMAKE_EXPORT_COMPILE_COMMANDS=ON'
        f' -DBUILD_TESTING={"ON" if testing else "OFF"}'
        f' -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache'
    )


COLCON_PKG = re.compile(r'^(Starting >>>|Finished <<<|Failed\s+<<<)\s+(\S+)')


def colcon_status(line):
    m = COLCON_PKG.match(line)
    if m and m.group(1).startswith('Starting'):
        return f'compiling {m.group(2)}'
    return None


def colcon_built(lines):
    n = sum(1 for x in lines if x.startswith('Finished <<<'))
    failed = [COLCON_PKG.match(x).group(2) for x in lines if x.startswith('Failed')]
    if failed:
        return f'{", ".join(failed)} failed'
    return f'{n} package{"s" if n != 1 else ""}'


def colcon_errors(lines):
    """Compiler / python errors of a failed colcon run, without the progress noise."""
    keep, inside = [], False
    for x in lines:
        if x.startswith('--- stderr:'):
            inside = True
        elif x.startswith('---'):
            inside = False
        elif inside or 'error' in x.lower():
            keep.append(x)
    return keep[-40:] or lines[-25:]


def check_dev_deps(ws, args, arch, domains):
    """Fail early, with the fix, when the dev image lacks a dependency of this checkout.

    The dev image installs the rosdep keys of the domains it was built from. A pulled
    image (DIT_DEV_TAG=develop) predates a package.xml change on your branch; colcon
    would then fail with an obscure "could not find package". A published image that
    fails the check is pulled again first (develop may have the dependency by now).
    Images older than the readable rosdep cache skip the check."""
    if cross_compiles(arch) or not domains:
        return
    paths = ' '.join(shlex.quote(p) for p in [f'{ws.root}/base', *map(ws.domain_path, domains)])
    script = (
        'H=/etc/ros/rosdep-home; [ -d "$H/rosdep" ] || exit 0\n'
        'source /opt/ros/$ROS_DISTRO/setup.bash\n'
        f'ROS_HOME=$H rosdep check --rosdistro $ROS_DISTRO --ignore-src -q --from-paths {paths}'
        ' 2>&1'
    )
    plan = [('deps', 'would check the dev image has every rosdep key')]
    r = dev_run(ws, args, script, arch=arch, plan=plan)
    if r.returncode == 0:
        return
    image = ws.dev_image()
    if not image.endswith(':local'):
        # ditx pulls a published image only when it is missing: the copy here may predate the
        # dependency someone merged since. Fetch the current one and check again.
        with ui.Task('dev image') as t:
            t.status(f'pulling {image} (this copy lacks dependencies; develop may have them)')
            pulled = run(['docker', 'pull', image], args, check=False).returncode == 0
            if pulled:
                t.done(f'pulled {image}')
            else:
                t.fail('pull failed')
        if pulled:
            r = dev_run(ws, args, script, arch=arch, plan=plan)
            if r.returncode == 0:
                return
    missing = [x for x in (r.stdout or '').splitlines() if x.strip()][-12:]
    build = 'ditx image --target dev'
    fix = (
        f'{build}  (dependencies changed since it was built)'
        if ws.dev_image().endswith(':local')
        else f'{build}, then DIT_DEV_TAG=local ditx build  (or set DIT_DEV_TAG=local in .env;'
        ' the published image only has the dependencies of develop)'
    )
    raise Fail(
        f'the dev image {ws.dev_image()} lacks dependencies of this checkout:\n'
        + '\n'.join(f'  {x}' for x in missing)
        + f'\n  Build your own: {fix}'
    )


def cmd_build(ws, args):
    arch = args.arch or native_arch()
    domains = pick_domains(ws, args.domains) if (args.domains or not args.base) else []
    workers, mj, mem = parse_jobs(args.jobs, getattr(args, 'workers', 'auto'))
    testing = not cross_compiles(arch)  # arm64 test binaries could not run here anyway
    ui.step(
        f'build  {ws.distro}/{arch} · {workers} workers × make -j{mj} · '
        f'RAM cap {mem} GB → build/{ws.build_tag(arch)}/'
    )
    sh = ['set -o pipefail', f'export MAKEFLAGS=-j{mj}', 'source /opt/ros/$ROS_DISTRO/setup.bash']
    rel = os.path.relpath
    plan = []
    if args.clean:
        sh += [f'rm -rf {ws.build_dir(arch, d)}' for d in domains]
        plan += [('clean', f'would delete {rel(ws.build_dir(arch, d), ws.root)}') for d in domains]
    if args.base:
        bdir = ws.build_dir(arch, 'base')
        sh.append(
            section_sh(
                'base',
                colcon_build_sh(f'{ws.root}/base', bdir, workers, args.build_type, testing=testing)
                + f' && {ws.root}/docker/fingerprint.sh {ws.root}/base > {bdir}/FINGERPRINT',
            )
        )
        plan.append(('base', f'would colcon build base/ → {rel(bdir, ws.root)}'))
    sh.append(source_base_sh(ws, arch))
    built = []
    for d in domains:
        pkgs = domain_packages(ws, d, args.packages)
        if pkgs == []:
            continue
        bdir = ws.build_dir(arch, d)
        # CMake caches the interface paths: a different base underlay needs a fresh configure
        sh.append(
            f'old=$(cat {bdir}/BASE_FINGERPRINT 2>/dev/null); '
            f'if [ -n "$old" ] && [ "$old" != "$BASE_FP" ];'
            f' then echo "@@note {d}: base changed ($old → $BASE_FP),'
            f' rebuilding it from scratch"; rm -rf {bdir}/build {bdir}/install; fi'
        )
        sh.append(
            section_sh(
                d,
                colcon_build_sh(
                    ws.domain_path(d), bdir, workers, args.build_type, pkgs, testing=testing
                )
                + f' && echo "$BASE_FP" > {bdir}/BASE_FINGERPRINT',
            )
        )
        sel = f' (packages: {", ".join(pkgs)})' if pkgs else ''
        plan.append((d, f'would colcon build domains/{d}{sel} → {rel(bdir, ws.root)}'))
        built.append(d)
    if args.packages and not built and not args.base:
        raise Fail(f'none of {args.packages} found in {", ".join(domains)}')
    check_dev_deps(ws, args, arch, built)
    secs = Sections(colcon_status, colcon_built, colcon_errors)
    t0 = time.monotonic()
    r = dev_run(ws, args, '\n'.join(sh), arch=arch, mem_gb=mem, on_line=secs, plan=plan)
    secs.close()
    if r.returncode:
        raise Fail(f'build failed: {", ".join(secs.failed) or "see the log"}')
    if getattr(args, 'clangd', False):
        if args.dry_run:  # nothing was built, so there is nothing to merge yet
            ui.planned('index', 'would merge compile_commands.json for clangd')
        else:
            cmd_index(ws, argparse.Namespace(domains=built, dry_run=False))
    if not args.dry_run:
        ui.done(f'{len(secs.passed)} built · {time.monotonic() - t0:.0f}s')


TEST_SUMMARY = re.compile(r'Summary: (\d+) tests, (\d+) errors, (\d+) failures, (\d+) skipped')


def test_summary(lines):
    for x in reversed(lines):
        m = TEST_SUMMARY.search(x)
        if m:
            n, err, fail, skip = map(int, m.groups())
            out = f'{n} tests'
            if err + fail:
                out += f', {err + fail} failed'
            if skip:
                out += f', {skip} skipped'
            return out
    return ''


def test_status(line):
    m = COLCON_PKG.match(line)
    return f'testing {m.group(2)}' if m and m.group(1).startswith('Starting') else None


def test_failures(lines):
    bad = [x for x in lines if any(k in x for k in ('FAIL', 'Error', 'error:', 'assert'))]
    return bad[-30:] or lines[-25:]


def integration_status(line):
    if line.startswith('▸ started'):
        return line[2:].split(' (')[0]
    return None


def integration_summary(lines):
    out = []
    for x in lines:
        m = re.search(r'ready after ([\d.]+s)', x)
        if m and x.startswith('OK'):
            out.append(f'handshake {m.group(1)}')
        m = re.search(r'(\d+) topics checked', x)
        if m and x.startswith('OK'):
            out.append(f'{m.group(1)} topics match the contract')
    return ' · '.join(out)


def cmd_test(ws, args):
    arch = native_arch()
    domains = pick_domains(ws, args.domains)
    unsynced = [d for d in ws.domains() if d not in ws.synced_domains()]
    if args.integration and unsynced:
        # main waits for every domain in ditx.repos: fail now, not after the 45 s timeout
        raise Fail(
            f'the integration test starts every domain; not synced: {", ".join(unsynced)} '
            f'(run: ditx sync, or test without --integration)'
        )
    workers, mj, mem = parse_jobs('auto')
    sh = ['set -o pipefail', 'fail=0', f'export MAKEFLAGS=-j{mj}']
    # tests talk DDS only to themselves: a private domain on loopback, so a sim or a robot on
    # the network never mixes in, and extra interfaces (VPN, TUN, docker) cannot stall discovery
    sh += [
        'export ROS_DOMAIN_ID=$((RANDOM % 100 + 100))',
        'export CYCLONEDDS_URI=file:///opt/eurobot/cyclonedds-localhost.xml',
        'export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST',
    ]
    sh.append('source /opt/ros/$ROS_DISTRO/setup.bash')
    if args.base:
        # separate dir: testing base must not turn into a local base override for builds
        bdir = ws.build_dir(arch, 'base-test')
        sh.append(
            section_sh(
                'base',
                colcon_build_sh(f'{ws.root}/base', bdir, workers, 'RelWithDebInfo')
                + f' && colcon --log-base {bdir}/log test --base-paths {ws.root}/base '
                f'--build-base {bdir}/build --install-base {bdir}/install --merge-install'
                f' && colcon --log-base {bdir}/log test-result --test-result-base {bdir}/build'
                f' --verbose',
                stop=False,
            )
        )
    sh.append(source_base_sh(ws, arch))
    plan = [('base', 'would build and test base/')] if args.base else []
    # unit tests always; --integration adds the system test on top
    for d in domains:
        pkgs = domain_packages(ws, d, args.packages)
        if pkgs == []:
            continue
        bdir = ws.build_dir(arch, d)
        sel = f' --packages-select {" ".join(pkgs)}' if pkgs else ''
        sh.append(
            section_sh(
                d,
                f'if [ ! -d {bdir}/install ]; then echo "{d} is not built: ditx build -d {d}"; '
                f'exit 1; fi\n'
                f'colcon --log-base {bdir}/log test --base-paths {ws.domain_path(d)} '
                f'--build-base {bdir}/build --install-base {bdir}/install --merge-install{sel}'
                f' && colcon --log-base {bdir}/log test-result --test-result-base {bdir}/build'
                f' --verbose',
                stop=False,
            )
        )
        which = f' (packages: {", ".join(pkgs)})' if pkgs else ''
        plan.append((d, f'would run the unit tests of domains/{d}{which}'))
    unit = Sections(test_status, test_summary, test_failures)
    integ = Sections(integration_status, integration_summary, lambda ls: ls[-40:])
    if args.integration:
        installs = ' '.join(f'{ws.build_dir(arch, d)}/install' for d in domains)
        sh.append(
            'echo "@@integration"\n'
            + section_sh(
                'integration',
                f'{ws.root}/tools/integration_test.sh '
                f'{shlex.quote(ws.build_dir(arch, "integration"))} '
                f'{shlex.quote(",".join(domains))} {installs}',
                stop=False,
            )
        )
        plan.append(('integration', 'would start every domain and check the handshake + contract'))
    sh.append('exit $fail')
    current = [unit]

    def on_line(line):
        if line == '@@integration':
            current[0] = integ
            return
        current[0](line)

    t0 = time.monotonic()
    r = dev_run(ws, args, '\n'.join(sh), mem_gb=mem, on_line=on_line, plan=plan)
    unit.close()
    integ.close()
    failed = unit.failed + integ.failed
    if r.returncode or failed:
        raise Fail(f'tests failed: {", ".join(failed) or "see the log"}')
    if not args.dry_run:
        ui.done(f'all tests passed · {time.monotonic() - t0:.0f}s')


def count_files(ws, targets):
    """(C++ files in the targets, shell scripts incl. docker/ and infra/) for the dry-run plan."""
    cpp = shf = 0
    for t in [*targets, 'docker', 'infra']:
        for dirpath, dirnames, files in os.walk(os.path.join(ws.root, t)):
            dirnames[:] = [d for d in dirnames if d != 'build' and not d.startswith('.')]
            if t not in ('docker', 'infra'):
                cpp += sum(f.endswith(('.cpp', '.hpp', '.h', '.cc')) for f in files)
            shf += sum(f.endswith('.sh') or dirpath.endswith('tools/shell') for f in files)
    return cpp, shf + 1  # + install.sh


def lint_targets(ws, domains):
    """Paths relative to the workspace root (the container's working directory)."""
    return ['base', 'tools'] + [os.path.relpath(ws.domain_path(d), ws.root) for d in domains]


def lint_summary(lines):
    """One phrase for a lint section: what it checked, or how many problems it found."""
    text = '\n'.join(lines)
    for prefix, phrase in (('needs formatting:', 'need formatting'), ('formatted:', 'reformatted')):
        files = [x.split(':', 1)[1].strip() for x in lines if x.startswith(prefix)]
        if files:
            names = ', '.join(os.path.basename(f) for f in files[:3])
            more = f' +{len(files) - 3}' if len(files) > 3 else ''
            return f'{len(files)} file{"s" if len(files) > 1 else ""} {phrase}: {names}{more}'
    for pattern, phrase in (
        (r'Found (\d+) errors? \((\d+) fixed', '{} problems, {} fixed'),
        (r'Found (\d+) errors?', '{} problems'),
        (r'(\d+) files? would be reformatted', '{} files need formatting'),
        (r'(\d+) files? reformatted', '{} files reformatted'),
        (r'(\d+) boundary violation', '{} boundary violations'),
    ):
        m = re.search(pattern, text)
        if m:
            return phrase.format(*m.groups())
    issues = sum(1 for x in lines if x.startswith('In '))
    if issues:
        return f'{issues} issues'
    for x in reversed(lines):
        x = x.strip()
        if x and len(x) < 60 and x[0].isalnum():
            return x
    return ''


def lint_problems(lines):
    """The findings only: no counters like `13 files`, no `All checks passed!`."""
    skip = re.compile(
        r'^(\d+ (files?|scripts?)( .*)?|All checks passed!|Found \d+ errors?.*|valid)$'
    )
    return [x for x in lines if x.strip() and not skip.match(x.strip())][:40]


def cmd_lint(ws, args):
    domains = [d for d in (args.domains or ws.synced_domains())]
    paths = ' '.join(shlex.quote(p) for p in lint_targets(ws, domains))
    fix = args.fix
    find_cpp = (
        f"find {paths} -path '*/build' -prune -o -type f "
        "\\( -name '*.cpp' -o -name '*.hpp' -o -name '*.h' -o -name '*.cc' \\) -print"
    )
    find_sh = (  # + the shell rc of `ditx shell` and the root install.sh
        f"find {paths} {ws.root}/docker {ws.root}/infra {ws.root}/install.sh -path '*/build' "
        "-prune -o -type f \\( -name '*.sh' -o -path '*tools/shell/*' \\) -print"
    )
    gen = f'{ws.root}/base/eurobot_common/scripts/gen_contract.py'
    # caches live under build/ like everything generated, never in the workspace root
    sh = ['set -o pipefail', 'fail=0', f'export RUFF_CACHE_DIR={ws.root}/build/.ruff_cache']
    n_cpp, n_sh = count_files(ws, lint_targets(ws, domains))
    verb = 'would format' if fix else 'would check'
    plan = [
        ('clang-format', f'{verb} {n_cpp} C++ files'),
        ('ruff', f'{verb} the Python in base, tools and {len(domains)} domains'),
        ('shellcheck', f'would check {n_sh} shell scripts'),
        ('contract', 'would validate base/eurobot_common/contract.yaml'),
        ('boundaries', 'would check that domains depend only on base'),
    ]
    sh.append(
        section_sh(
            'clang-format',
            f'files=$({find_cpp}); n=0; bad=0\n'
            'for f in $files; do n=$((n+1)); clang-format --dry-run -Werror "$f" >/dev/null 2>&1 '
            + (
                '|| { clang-format -i "$f"; echo "formatted: $f"; }; done\n'
                if fix
                else '|| { echo "needs formatting: $f"; bad=$((bad+1)); }; done\n'
            )
            + 'echo "$n files"; [ $bad -eq 0 ]',
            stop=False,
        )
    )
    sh.append(
        section_sh(
            'ruff',
            f'ruff check --output-format concise {"--fix " if fix else ""}{paths} && '
            f'ruff format {"" if fix else "--check "}{paths}',
            stop=False,
        )
    )
    sh.append(
        section_sh(
            'shellcheck',
            f'files=$({find_sh}); [ -z "$files" ] || echo "$files" | xargs shellcheck -x '
            '&& echo "$(echo "$files" | grep -c .) scripts"',
            stop=False,
        )
    )
    sh.append(
        section_sh(
            'contract',
            f'python3 {gen} {ws.root}/base/eurobot_common/contract.yaml --check && echo valid',
            stop=False,
        )
    )
    sh.append(
        section_sh(
            'boundaries',
            f'python3 {ws.root}/tools/check_boundaries.py {ws.root} {" ".join(domains)} '
            f'| sed "s/^ok$/no cross-domain dependencies/"',
            stop=False,
        )
    )
    sh.append('exit $fail')
    secs = Sections(None, lint_summary, lint_problems)
    r = dev_run(ws, args, '\n'.join(sh), on_line=secs, plan=plan)
    secs.close()
    if r.returncode or secs.failed:
        raise Fail(
            f'lint: {", ".join(secs.failed)}'
            + ('' if fix else '   (formatting is fixed by: ditx lint --fix)')
        )
    if fix:
        ui.done('files were rewritten in place: review with git diff')


def cmd_index(ws, args):
    arch = native_arch()
    domains = pick_domains(ws, args.domains)
    entries = []
    for name in ['base'] + domains:
        bdir = os.path.join(ws.build_dir(arch, name), 'build')
        if not os.path.isdir(bdir):
            continue
        for pkg in sorted(os.listdir(bdir)):
            f = os.path.join(bdir, pkg, 'compile_commands.json')
            if os.path.isfile(f):
                with open(f, encoding='utf-8') as fh:
                    entries += json.load(fh)
    if not entries:
        raise Fail('no compile_commands.json yet; run ditx build first')
    out = os.path.join(ws.root, 'compile_commands.json')
    if not args.dry_run:
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(entries, f, indent=1)
    with ui.Task('index') as t:
        verb = 'would write' if args.dry_run else 'wrote'
        t.status(f'{verb} {len(entries)} translation units → compile_commands.json')
    ui.info('clangd picks it up; ROS headers resolve inside the VS Code Dev Container')


def cmd_shell(ws, args):
    arch = args.arch or native_arch()
    src = [source_base_sh(ws, arch)]
    for d in ws.synced_domains():
        f = os.path.join(ws.build_dir(arch, d), 'install', 'setup.bash')
        src.append(f'[ -f {f} ] && source {f}')
    rc = f'{ws.root}/tools/shell/devrc'
    script = '\n'.join(src) + '\n' + (args.cmd or f'exec bash --rcfile {shlex.quote(rc)} -i')
    if args.cmd:
        plan = [('shell', f'would run: {args.cmd}')]
        r = dev_run(ws, args, script, arch=arch, plan=plan, emulate=True)
        if r.stdout:
            print(r.stdout)
        return r.returncode
    plan = [('shell', 'would open an interactive shell in the dev container')]
    r = dev_run(ws, args, script, arch=arch, interactive=True, plan=plan, emulate=True)
    return r.returncode


TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')


def cmd_new(ws, args):
    domain, pkg = args.domain, args.package
    if domain not in ws.synced_domains():
        raise Fail(f'domain {domain!r} is not synced')
    if not re.fullmatch(r'[a-z][a-z0-9_]*', pkg or ''):
        raise Fail('package names are snake_case: lowercase letters, digits, underscores (REP 144)')
    if not pkg.startswith(domain + '_'):
        raise Fail(
            f'prefix the package with its domain, e.g. {domain}_{pkg} (keeps names unique across '
            f'repos)'
        )
    dst = os.path.join(ws.domain_path(domain), 'src', pkg)
    if os.path.exists(dst):
        raise Fail(f'{dst} already exists')
    node = pkg[len(domain) + 1 :] or pkg
    subst = {
        '__PKG__': pkg,
        '__DOMAIN__': domain,
        '__NODE__': f'{node}_node',
        '__CLASS__': ''.join(w.title() for w in node.split('_')) + 'Node',
        '__MODULE__': node,
    }
    src = os.path.join(TEMPLATES, args.lang)
    planned = []
    for dirpath, _dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        for name in files:
            target_rel = os.path.join(rel, name)
            for k, v in subst.items():
                target_rel = target_rel.replace(k, v)
            target = os.path.normpath(os.path.join(dst, target_rel))
            with open(os.path.join(dirpath, name), encoding='utf-8') as f:
                text = f.read()
            for k, v in subst.items():
                text = text.replace(k, v)
            if args.dry_run:
                planned.append(os.path.relpath(target, dst))
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8') as f:
                f.write(text)
            if name.endswith('.py') and '/scripts' in target:
                os.chmod(target, 0o755)
    if args.dry_run:
        where = os.path.relpath(dst, ws.root)
        ui.planned(pkg, f'would create {where}/ from the {args.lang} template')
        ui.planned('files', ', '.join(sorted(planned)))
        ui.planned('format', 'would run clang-format + ruff on it (like ditx lint --fix)')
    else:
        # names change line lengths and import order: format the result like `ditx lint --fix`
        with ui.Task(pkg) as t:
            t.status('formatting')
            r = dev_run(
                ws,
                args,
                f'cd {shlex.quote(dst)} && '
                "find . -name '*.cpp' -o -name '*.hpp' | xargs -r clang-format -i && "
                f'export RUFF_CACHE_DIR={ws.root}/build/.ruff_cache && '
                'ruff check --fix --quiet . && ruff format --quiet .',
            )
            if r.returncode:
                ui.show_tail(r.stdout.splitlines())
            t.status(f'{args.lang} package in {os.path.relpath(dst, ws.root)}')
    ui.info(
        f'next: add it to {domain}_bringup/launch/bringup.launch.py, then  ditx build -d {domain}'
    )


# ════════════════════════════════════════════════════════════════════════════
# robot: overlay deploy / rollback / runtime stack
# ════════════════════════════════════════════════════════════════════════════


# everything ditx keeps on a robot; next to DIT-Scripts' /home/share/{data,scripts}
ROBOT_ROOT = '/home/share/eurobot'


class Target:
    """Where a robot's runtime lives. `host = local` simulates a robot under .sim-robot/."""

    def __init__(self, ws, robot, args):
        self.ws, self.robot, self.args = ws, robot, args
        # same layout on the sim and on a real robot (infra/robot/install.sh):
        #   compose.yaml .env  overlays/  active/ (--persist)  run/active/ (emptied at boot)  logs/
        base = os.path.join(ws.root, '.sim-robot') if robot.is_local else ROBOT_ROOT
        self.home = self.lib_dir = base
        self.run_dir, self.log_dir = f'{base}/run', f'{base}/logs'

    @property
    def label(self):
        return f'{self.robot.name} ({"this machine" if self.robot.is_local else self.robot.host})'

    def _cmd(self, script):
        # group-writable files: every teammate in the robot's docker group can deploy
        # over (and prune) what someone else pushed
        script = 'umask 002\n' + script
        if self.robot.is_local:
            return ['bash', '-c', script]
        return [
            'ssh',
            '-o',
            'BatchMode=yes',
            '-o',
            'ConnectTimeout=5',
            self.robot.host,
            'bash -c ' + shlex.quote(script),
        ]

    def sh(self, script, data=None, check=True, log=True, stream=False, echo=None):
        """Run a script on the robot. Output goes to the log (stream=True: to the terminal).

        A dry run echoes the full script unless echo=False (internal state queries).
        """
        first = script.strip().split('\n')[0] + (' …' if '\n' in script.strip() else '')
        where = '' if self.robot.is_local else f'[{self.robot.host}] '
        if self.args.dry_run and echo is not False:
            ui.command(where + script.strip(), force=True)
        elif log:
            ui.command(where + first)
        cmd = self._cmd(script)
        if self.args.dry_run:
            return subprocess.CompletedProcess(cmd, 0, '', '')
        if stream:
            return subprocess.run(cmd)
        r = subprocess.run(cmd, input=data, capture_output=True)
        out = (r.stdout + r.stderr).decode(errors='replace')
        if log:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, 'a', encoding='utf-8') as f:
                f.write(f'$ {where}{first}\n{out}')
            if ui.VERBOSE and out.strip():
                print(out.rstrip(), file=sys.stderr)
        if check and r.returncode:
            if not ui.VERBOSE:
                ui.show_tail(out.splitlines())
            raise Fail(f'on {self.label}: exit code {r.returncode}')
        return subprocess.CompletedProcess(cmd, r.returncode, r.stdout.decode(errors='replace'), '')

    def compose(self, rest):
        return (
            f'docker compose --project-directory {self.home} -f {self.home}/compose.yaml '
            f'--env-file {self.home}/.env {rest}'
        )

    def write_stack_files(self):
        """compose.yaml + .env on the robot, generated from config/robots.ini."""
        r = self.robot
        localhost = r.discovery == 'localhost'
        env = {
            'COMPOSE_PROJECT_NAME': 'eurobot-sim' if r.is_local else 'eurobot',
            'COMPOSE_PROFILES': ','.join(r.profiles),
            'ROBOT': r.name,
            'ROS_DOMAIN_ID': r.domain_id,
            'ROS_DISTRO': self.ws.distro,
            'REGISTRY': self.ws.registry,
            'TAG': r.tag,
            # same localhost discovery on every distro (Humble's LOCALHOST_ONLY would not
            # find the tools that join the sim); Jazzy's range setting agrees with it
            'ROS_AUTOMATIC_DISCOVERY_RANGE': 'LOCALHOST' if localhost else 'SUBNET',
            'CYCLONEDDS_URI': 'file:///opt/eurobot/cyclonedds-'
            + ('localhost.xml' if localhost else 'xml')[: None if localhost else None],
            'EUROBOT_RUN_DIR': self.run_dir,
            'EUROBOT_LIB_DIR': self.lib_dir,
            'EUROBOT_LOG_DIR': self.log_dir,
        }
        lines = (
            ['# written by ditx from config/robots.ini; local edits are overwritten']
            + [f'{k}={v}' for k, v in env.items()]
            # containers run as the deploying user in the folder's group (docker on a robot)
            + ['EUROBOT_UID=$(id -u)', f'EUROBOT_GID=$(stat -c %g {self.home})']
        )
        with open(os.path.join(self.ws.root, 'docker', 'compose.yaml'), encoding='utf-8') as f:
            compose = f.read()
        script = (
            f'set -e; mkdir -p {self.home} {self.run_dir}/active {self.lib_dir}/overlays '
            f'{self.lib_dir}/active {self.log_dir}\n'
            f"cat > {self.home}/compose.yaml <<'DIT_EOF'\n{compose}DIT_EOF\n"
            f'cat > {self.home}/.env <<DIT_EOF\n' + '\n'.join(lines) + '\nDIT_EOF\n'
        )
        if self.args.dry_run:
            ui.command(
                f'# write {self.home}/compose.yaml (docker/compose.yaml) and '
                f'{self.home}/.env (from config/robots.ini)',
                force=True,
            )
        self.sh(script, log=False, echo=False)

    def read(self, script):
        return self.sh(script, check=False, log=False, echo=False).stdout or ''

    def running_service(self):
        """A domain container that is up, to run ROS commands in; Fail with the fix if none."""
        svcs = [d for d in self.robot.profiles if d in self.ws.domains()]
        if not svcs:
            raise Fail(f'{self.robot.name} runs no domain container')
        if self.args.dry_run:
            return svcs[0]
        ps = self.compose('ps --services --status running')
        up = self.read(f'test -f {self.home}/.env && {ps}').split()
        svc = next((s for s in svcs if s in up), None)
        if not svc:
            raise Fail(f'{self.robot.name} is not up  → ditx up -r {self.robot.name}')
        return svc


DIT_HOST = re.compile(r'(?i)dit-(\d{4})-(\d{1,3})(\.local)?')  # DIT-Scripts: DIT-<year>-<ip octet>


def resolve_host(host, user=None):
    """(host for robots.ini, domain id or None). Expands DIT-Scripts hostnames:
    DIT-2026-11 -> (dit-2026-11.local, 11). No login is stored unless one is given:
    everyone connects with their own team account (DIT_ROBOT_USER, see Workspace.robots)."""
    login, _, name = host.rpartition('@')
    login = login or user
    m = DIT_HOST.fullmatch(name)
    if m:
        name = f'dit-{m[1]}-{m[2]}.local'
    return (f'{login}@{name}' if login else name), (m[2] if m else None)


def robot_env_domain_id(host):
    """ROS_DOMAIN_ID in the robot's /etc/environment (DIT-Scripts: the hostname number),
    or None when it is unset or the robot does not answer."""
    r = subprocess.run(
        [
            'ssh',
            '-o',
            'BatchMode=yes',
            '-o',
            'ConnectTimeout=2',
            host,
            "sed -n 's/^ROS_DOMAIN_ID=//p' /etc/environment",
        ],
        capture_output=True,
        text=True,
    )
    value = r.stdout.strip().strip('"') if r.returncode == 0 else ''
    return value or None


def get_robot(ws, name):
    robots = ws.robots()
    name = name or ws.default_robot()
    if name not in robots:
        raise Fail(
            f'unknown robot {name!r}; robots in config/robots.ini: {", ".join(robots) or "(none)"}'
        )
    return robots[name]


def git_info(ws, path):
    return {
        'commit': ws.git(path, 'rev-parse', 'HEAD'),
        'branch': ws.git(path, 'rev-parse', '--abbrev-ref', 'HEAD'),
        'dirty': bool(ws.git(path, 'status', '--porcelain')),  # untracked files count too
        'describe': ws.git(path, 'describe', '--always', '--dirty'),
    }


def git_user(ws):
    return ws.git(ws.root, 'config', 'user.name') or getpass.getuser()


def make_manifest(ws, domain, robot, arch):
    bdir = ws.build_dir(arch, domain)
    try:
        with open(os.path.join(bdir, 'BASE_FINGERPRINT')) as f:
            base_fp = f.read().strip()
    except OSError:
        if ui.DRY_RUN:  # the dry-run build above did not build anything
            base_fp = '(not built)'
        else:
            raise Fail(  # noqa: B904 - the OSError itself says nothing useful to the user
                f'{domain} has no build for {ws.build_tag(arch)} '
                f'(run: ditx build -d {domain} --arch {arch})'
            )
    g = git_info(ws, ws.domain_path(domain))
    user = git_user(ws)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    safe_user = re.sub(r'[^A-Za-z0-9]+', '-', user).strip('-').lower() or 'someone'
    oid = f'{stamp}-{safe_user}-{(g["commit"] or "nogit")[:7]}{"-dirty" if g["dirty"] else ""}'
    return oid, {
        'id': oid,
        'domain': domain,
        'robot': robot.name,
        'user': user,
        'host': socket.gethostname(),
        'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'ros_distro': ws.distro,
        'arch': arch,
        'base_fingerprint': base_fp,
        'git': {domain: g, 'shell': git_info(ws, ws.root)},
    }


def pack_overlay(ws, domain, arch, manifest):
    bdir = ws.build_dir(arch, domain)
    with tempfile.SpooledTemporaryFile(max_size=64 << 20) as buf:
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            tar.add(os.path.join(bdir, 'install'), arcname='install')
            data = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo('manifest.json')
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return buf.read()


def active_overlay(t, domain):
    return t.read(
        f'cat {t.run_dir}/active/{domain} 2>/dev/null || cat {t.lib_dir}/active/{domain} '
        f'2>/dev/null'
    ).strip()


def set_active(t, domain, oid, persist):
    if not oid:
        t.sh(f'rm -f {t.run_dir}/active/{domain} {t.lib_dir}/active/{domain}')
    elif persist:
        t.sh(f'echo {oid} > {t.lib_dir}/active/{domain} && rm -f {t.run_dir}/active/{domain}')
    else:
        t.sh(f'echo {oid} > {t.run_dir}/active/{domain}')


def recreate(t, domain):
    t.sh(t.compose(f'up -d --force-recreate --no-deps {domain}'))


def verify(t, domain, oid, since, settle=6, timeout=30):
    """The container must report the overlay (or the stable image) and then stay up `settle` s."""
    if t.args.dry_run:
        return True, ''
    marker = f'overlay {oid} active' if oid else 'running stable image'
    start, logs, seen_at = time.monotonic(), '', None  # steady clock: timeouts never jump
    while time.monotonic() - start < timeout:
        time.sleep(1.5)
        state = t.read(
            f"docker inspect -f '{{{{.State.Status}}}} {{{{.RestartCount}}}}' "
            f"$({t.compose(f'ps -aq {domain}')}) 2>/dev/null"
        ).strip()
        logs = t.read(t.compose(f'logs --no-color --no-log-prefix --since {since} {domain}'))
        if state and (not state.startswith('running') or state.split()[-1] != '0'):
            return False, logs
        refused = (f'overlay {oid} was built against', f'overlay {oid} not found')
        if oid and any(x in logs for x in refused):
            return False, logs
        if 'process has died' in logs:  # a node crashed; ros2 launch itself keeps running
            return False, logs
        if marker in logs and seen_at is None:
            seen_at = time.monotonic()
        if seen_at is not None and time.monotonic() - seen_at >= settle:
            return True, logs
    return False, logs


def readable_log(logs):
    """Container log lines for a failure report: the one-line manifest JSON shortened."""
    return [x.split(': {', 1)[0] if ' active: {' in x else x for x in logs.strip().split('\n')]


def cmd_deploy(ws, args):
    robot = get_robot(ws, args.robot)
    if not args.domains:
        raise Fail('say which domains to deploy: ditx deploy -d <domain> [-r robot]')
    domains = pick_domains(ws, args.domains)
    not_running = [d for d in domains if d not in robot.profiles]
    if not_running:
        raise Fail(
            f'{robot.name} does not run {", ".join(not_running)} (profiles in config/robots.ini)'
        )
    arch = robot.arch
    if not args.no_build:
        cmd_build(
            ws,
            argparse.Namespace(
                domains=domains,
                packages=[],
                base=False,
                arch=arch,
                jobs=args.jobs,
                workers='auto',
                clangd=False,
                build_type='RelWithDebInfo',
                clean=False,
                dry_run=args.dry_run,
            ),
        )
    t = Target(ws, robot, args)
    ui.step(
        f'deploy → {t.label} · '
        + ('persists across reboot' if args.persist else 'gone after reboot (stable image)')
    )
    t.write_stack_files()
    failed = []
    for d in domains:
        task = ui.Task(d)
        oid, manifest = make_manifest(ws, d, robot, arch)
        g = manifest['git'][d]
        if g['dirty']:
            task.print(
                f'{ui.paint("!", ui.WARN, bold=True)} {d} has uncommitted changes: '
                f'the overlay id ends in -dirty so everyone can see it'
            )
        task.status('checking interfaces against the robot image')
        image_fp = t.read(
            t.compose(f'run --rm --no-deps --entrypoint cat {d} /opt/eurobot/base/FINGERPRINT')
        ).strip()
        if image_fp and image_fp != manifest['base_fingerprint']:
            msg = (
                f'built against interfaces {manifest["base_fingerprint"]}, robot image has '
                f'{image_fp}: rebuild against the robot image (or update it), or --allow-skew'
            )
            if not args.allow_skew:
                task.fail('interfaces differ from the robot image')
                raise Fail(f'{d}: {msg}')
            task.print(f'{ui.paint("!", ui.WARN, bold=True)} {d}: {msg}')
        previous = active_overlay(t, d)
        if args.dry_run:
            task.done(f'would push overlay {oid}')
            continue
        task.status('packing')
        payload = pack_overlay(ws, d, arch, manifest)
        size = f'{len(payload) / 1e6:.1f} MB'
        task.status(f'uploading {size}')
        t.sh(
            f'set -e; d={t.lib_dir}/overlays/{d}/{oid}; rm -rf "$d"; mkdir -p "$d"; '
            f'tar -xzf - -C "$d"; chmod -R g+w "$d"',  # teammates can prune it later
            data=payload,
        )
        if args.allow_skew:
            t.sh(
                f"sed -i '/^EUROBOT_ALLOW_SKEW=/d' {t.home}/.env; "
                f'echo EUROBOT_ALLOW_SKEW=1 >> {t.home}/.env'
            )
        set_active(t, d, oid, args.persist)
        since = t.read('date +%s').strip() or str(int(time.time()))
        task.status('restarting')
        recreate(t, d)
        task.status('verifying: overlay active and nodes stay up')
        good, logs = verify(t, d, oid, since)
        if good:
            task.done(f'{oid}  {size}  verified')
            ui.info(f'{"":{ui.LABEL_W + 2}}by {manifest["user"]} from {g["describe"]}')
            # keep the 5 newest overlays per domain, never the active one
            t.sh(
                f'cd {t.lib_dir}/overlays/{d} && ls -1t | tail -n +6 | grep -vx {oid} | xargs -r '
                f'rm -rf',
                check=False,
                log=False,
            )
            continue
        failed.append(d)
        task.fail(f'{oid} did not come up')
        ui.show_tail(readable_log(logs), title='container log:')
        t.sh(f'touch {t.lib_dir}/overlays/{d}/{oid}/FAILED', check=False, log=False)
        if args.no_rollback:
            ui.warn('--no-rollback: leaving it running for debugging')
        else:
            with ui.Task('rollback') as rb:
                rb.status(f'{d} → {previous or "stable image"}')
                set_active(t, d, previous, persist=False)
                recreate(t, d)
                rb.status(f'{d} back on {previous or "the stable image"}')
    if failed:
        raise Fail(f'deploy failed for {", ".join(failed)}')


def cmd_rollback(ws, args):
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    domains = args.domains or [d for d in robot.profiles if d in ws.domains()]
    for d in domains:
        with ui.Task(d) as task:
            if args.to:
                exists = t.read(
                    f'test -f {t.lib_dir}/overlays/{d}/{args.to}/manifest.json && echo y'
                ).strip()
                if exists != 'y' and not args.dry_run:
                    task.fail(f'no overlay {args.to}')
                    raise Fail(f'{d}: no overlay {args.to} on {robot.name} (see: ditx overlays)')
            task.status('restarting')
            set_active(t, d, args.to, persist=False)
            if args.dry_run:
                recreate(t, d)
                task.status(f'would switch to {args.to or "the stable image"} and verify it')
                continue
            since = t.read('date +%s').strip() or str(int(time.time()))
            recreate(t, d)
            task.status('verifying')
            good, logs = verify(t, d, args.to, since)
            if not good:
                task.fail(f'{args.to or "stable image"} did not come up')
                ui.show_tail(readable_log(logs), title='container log:')
                raise Fail(f'{d}: rollback target is not healthy either; check the robot')
            task.status(f'now running {args.to or "the stable image"} · verified')


def cmd_overlays(ws, args):
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    raw = t.read(
        f'for f in {t.lib_dir}/overlays/*/*/manifest.json; do [ -f "$f" ] || continue; '
        f'tr -d "\\n" < "$f"; [ -f "$(dirname "$f")/FAILED" ] && echo " FAILED" || echo; done'
    )
    rows = []
    for line in raw.splitlines():
        bad = line.endswith(' FAILED')
        try:
            m = json.loads(line[: -len(' FAILED')] if bad else line)
            m['failed'] = bad
        except json.JSONDecodeError:
            continue
        if args.domains and m['domain'] not in args.domains:
            continue
        rows.append(m)
    active = {d: active_overlay(t, d) for d in {m['domain'] for m in rows}}
    persisted = {d: t.read(f'cat {t.lib_dir}/active/{d} 2>/dev/null').strip() for d in active}
    rows.sort(key=lambda m: (m['domain'], m['created']), reverse=False)
    if args.json:
        for m in rows:
            m['active'] = active.get(m['domain']) == m['id']
            m['persist'] = persisted.get(m['domain']) == m['id']
        print(json.dumps(rows, indent=1))
        return
    ui.table(
        [
            [
                m['domain'],
                ('◉ ' if active.get(m['domain']) == m['id'] else '  ') + m['id'],
                'failed'
                if m['failed']
                else 'persist'
                if persisted.get(m['domain']) == m['id']
                else '',
                m['user'],
                m['created'][:19].replace('T', ' '),
                m['git'][m['domain']]['describe'],
            ]
            for m in rows
        ],
        header=['DOMAIN', 'OVERLAY (◉ active)', '', 'BY', 'CREATED', 'COMMIT'],
    )
    if not rows:
        ui.info(f'no overlays on {robot.name}: everything runs the stable image')


def cmd_up(ws, args):
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    with ui.Task(robot.name) as task:
        task.status('writing compose.yaml + .env')
        t.write_stack_files()
        if args.pull:
            task.status('pulling images')
            t.sh(t.compose('pull'))
        task.status('starting containers')
        t.sh(t.compose('up -d --remove-orphans'))
        verb = 'would start' if args.dry_run else 'up:'
        task.status(f'{verb} {", ".join(robot.profiles)} · tag {robot.tag}')


def cmd_down(ws, args):
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    with ui.Task(robot.name) as task:
        task.status('stopping containers')
        t.sh(t.compose('down'))
        task.status('would stop every container' if args.dry_run else 'stopped')


def cmd_logs(ws, args):
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    if args.fetch and args.follow:
        raise Fail('--follow and --fetch do not go together: fetch copies files, follow streams')
    if args.fetch:
        dst = os.path.join(ws.root, 'logs', robot.name)
        os.makedirs(dst, exist_ok=True)
        with ui.Task('fetch logs') as task:
            if robot.is_local:
                run(['cp', '-a', t.log_dir + '/.', dst], args)
            else:
                run(
                    f'ssh {shlex.quote(robot.host)} tar -czf - -C {t.log_dir} . | tar -xzf - -C '
                    f'{shlex.quote(dst)}',
                    args,
                )
            task.status(f'{robot.name} → {os.path.relpath(dst, ws.root)}')
        return
    t.sh(
        t.compose(
            f'logs --tail 200 --no-log-prefix {"-f " if args.follow else ""}'
            f'{" ".join(args.domains)}'
        ),
        check=False,
        log=False,
        stream=True,
    )


def cmd_contract(ws, args):
    gen = f'{ws.root}/base/eurobot_common/scripts/gen_contract.py'
    yml = f'{ws.root}/base/eurobot_common/contract.yaml'
    if args.action in ('validate', 'md'):
        out = f'--md {ws.root}/docs/contract.md' if args.action == 'md' else '--check'
        script = f'python3 {gen} {yml} {out}'
        if args.dry_run:
            what = 'render docs/contract.md from' if args.action == 'md' else 'validate'
            dev_run(ws, args, script, plan=[('contract', f'would {what} contract.yaml')])
            return
        with ui.Task('contract') as task:
            r = dev_run(ws, args, script)
            if r.returncode:
                task.fail('invalid')
                ui.show_tail(r.stdout.splitlines())
                raise Fail('contract.yaml is invalid')
            task.status('docs/contract.md regenerated' if args.action == 'md' else 'valid')
        return
    robot = get_robot(ws, args.robot)
    t = Target(ws, robot, args)
    svc = t.running_service()
    with ui.Task('contract') as task:
        task.status(f'listening to the ROS graph of {robot.name}')
        r = t.sh(
            t.compose(
                f'exec -T -e EUROBOT_LOG_DIR= {svc} /opt/eurobot/entrypoint.sh '
                f'ros2 run eurobot_common contract_check --require all'
            ),
            check=False,
        )
        result = [x for x in r.stdout.splitlines() if x.startswith(('OK', 'FAIL'))]
        if args.dry_run:
            task.status(f'would compare the live graph of {robot.name} with contract.yaml')
            return
        if r.returncode:
            task.fail(f'{robot.name} differs from contract.yaml')
            for x in result:
                print(f'  {x}', file=sys.stderr)
            raise Fail('live graph does not match the contract')
        task.status(result[-1][6:].split(' against')[0] if result else 'ok')


# ════════════════════════════════════════════════════════════════════════════
# workspace
# ════════════════════════════════════════════════════════════════════════════


def repo_overrides(ws, refs):
    out = {}
    for d in ws.domains():
        v = os.environ.get(f'DIT_REF_{d.upper()}')
        if v:
            out[d] = v
    for r in refs or []:
        if '=' not in r:
            raise Fail(f'--ref wants DOMAIN=REF, got {r!r}')
        d, ref = r.split('=', 1)
        out[d] = ref
    return out


HTTPS_URL = re.compile(r'^https://([^/]+)/(.+?)(\.git)?$')
SSH_URL = re.compile(r'^(ssh://)?git@')


def clone_url(ws, url):
    """ditx.repos lists https URLs (CI and robots read them with a token); a workspace
    cloned over SSH gets the domains over SSH too, so pushing works with the same key."""
    shell = ws.git(ws.root, 'remote', 'get-url', 'origin')
    m = HTTPS_URL.match(url)
    if m and SSH_URL.match(shell or ''):
        return f'git@{m.group(1)}:{m.group(2)}.git'
    return url


def cmd_sync(ws, args):
    repos = ws.repos()
    if not repos:
        raise Fail(f'no repositories in {ws.repos_file}')
    refs = repo_overrides(ws, args.refs)
    want = set(args.domains)
    problems = []
    if not args.no_shell and not args.domains:
        sync_shell(ws, args, problems)
    install_hooks(ws, args)
    for path, spec in repos.items():
        name = path.split('/', 1)[-1]
        if want and name not in want:
            continue
        dst = os.path.join(ws.root, path)
        ver = refs.get(name, spec.get('version', ''))
        url = clone_url(ws, spec['url'])
        task = ui.Task(name)
        if not os.path.isdir(os.path.join(dst, '.git')):
            task.status(f'cloning {url}')
            run(['git', 'clone', '--quiet', url, dst], args)
            if ver:
                run(['git', '-C', dst, 'checkout', '--quiet', ver], args)
            if args.dry_run:
                task.done(f'would clone {url} @ {ver or "default branch"}')
                continue
            sha = ws.git(dst, 'rev-parse', '--short', 'HEAD')
            task.done(f'{"cloned":<14}{ver or "default branch"} @ {sha}')
            continue
        if ws.git(dst, 'status', '--porcelain', '--untracked-files=no'):
            task.skip('uncommitted changes, not touched (commit or stash, then sync again)')
            problems.append(name)
            continue
        # an earlier https clone of the manifest URL follows the shell repo to SSH
        if url != spec['url'] and ws.git(dst, 'remote', 'get-url', 'origin') == spec['url']:
            run(['git', '-C', dst, 'remote', 'set-url', 'origin', url], args)
        task.status('fetching')
        before = ws.git(dst, 'rev-parse', 'HEAD')
        run(['git', '-C', dst, 'fetch', '--quiet', '--tags', 'origin'], args)
        if ver:
            run(['git', '-C', dst, 'checkout', '--quiet', ver], args)
        branch = ws.git(dst, 'symbolic-ref', '-q', '--short', 'HEAD')
        if branch and ws.git(dst, 'rev-parse', '--abbrev-ref', '@{u}'):
            r = run(['git', '-C', dst, 'merge', '--ff-only', '--quiet', '@{u}'], args, check=False)
            if r.returncode:
                task.skip('diverged from its upstream, not touched (rebase or merge by hand)')
                problems.append(name)
                continue
        if args.dry_run:
            task.done(f'would fetch and fast-forward {branch or ver}')
            continue
        after = ws.git(dst, 'rev-parse', 'HEAD')
        n = ws.git(dst, 'rev-list', '--count', f'{before}..{after}') if before != after else '0'
        what = f'{n} new commit{"s" if n != "1" else ""}' if n != '0' else 'up to date'
        task.done(f'{what:<14}{branch or ver} @ {after[:7]}')
    install_hooks(ws, args)
    if problems:
        ui.warn(f'not synced: {", ".join(problems)}')


def sync_shell(ws, args, problems):
    """Fast-forward the shell repo itself (tooling, CI, interfaces) like robo sync does."""
    task = ui.Task('(shell)')
    root = ws.root
    branch = ws.git(root, 'symbolic-ref', '-q', '--short', 'HEAD')
    upstream = ws.git(root, 'rev-parse', '--abbrev-ref', '@{u}') if branch else ''
    if not upstream:
        task.skip('no upstream branch, not touched')
        return
    if ws.git(root, 'status', '--porcelain', '--untracked-files=no'):
        task.skip('uncommitted changes, not touched')
        problems.append('(shell)')
        return
    task.status('fetching')
    before = ws.git(root, 'rev-parse', 'HEAD')
    run(['git', '-C', root, 'fetch', '--quiet', 'origin'], args)
    merge = ['git', '-C', root, 'merge', '--ff-only', '--quiet', '@{u}']
    if args.dry_run:
        run(merge, args, check=False)
        task.done(f'would fetch and fast-forward {branch}')
        return
    if run(merge, args, check=False).returncode:
        task.skip('diverged from its upstream, not touched (rebase or merge by hand)')
        problems.append('(shell)')
        return
    after = ws.git(root, 'rev-parse', 'HEAD')
    n = ws.git(root, 'rev-list', '--count', f'{before}..{after}') if before != after else '0'
    what = f'{n} new commit{"s" if n != "1" else ""}' if n != '0' else 'up to date'
    task.done(f'{what:<14}{branch} @ {after[:7]}')  # same columns as the domain rows


def install_hooks(ws, args):
    """Point every repo of the workspace at tools/hooks (commit message check), once."""
    hooks = os.path.join(ws.root, 'tools', 'hooks')
    for path in [ws.root] + [ws.domain_path(d) for d in ws.synced_domains()]:
        if not os.path.isdir(os.path.join(path, '.git')) or args.dry_run:
            continue
        if ws.git(path, 'config', '--get', 'core.hooksPath') != hooks:
            subprocess.run(
                ['git', '-C', path, 'config', 'core.hooksPath', hooks], capture_output=True
            )


def repo_state(ws, name, path, fetch):
    """{repo, branch, commit, ahead, behind, changed, upstream, state} of one repo."""
    row = {
        'repo': name,
        'branch': '',
        'commit': '',
        'ahead': 0,
        'behind': 0,
        'changed': 0,
        'upstream': False,
        'synced': os.path.isdir(os.path.join(path, '.git')),
    }
    if not row['synced']:
        return {**row, 'state': 'not synced (ditx sync)'}
    if fetch:
        subprocess.run(['git', '-C', path, 'fetch', '--quiet'], capture_output=True, timeout=30)
    row['branch'] = ws.git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    counts = ws.git(path, 'rev-list', '--left-right', '--count', 'HEAD...@{u}')
    if counts:
        row['upstream'] = True
        row['ahead'], row['behind'] = (int(x) for x in counts.split())
    row['changed'] = len([x for x in ws.git(path, 'status', '--porcelain').splitlines() if x])
    row['commit'] = ws.git(path, 'rev-parse', '--short', 'HEAD')
    note = []
    if row['behind']:
        note.append(f'{row["behind"]} behind')
    if row['ahead']:
        note.append(f'{row["ahead"]} to push')
    if row['changed']:
        note.append(f'{row["changed"]} changed')
    if not row['upstream'] and row['branch'] != 'HEAD':
        note.append('no upstream')
    return {**row, 'state': ', '.join(note) or 'clean'}


def state_color(r):
    """git-like semantics: clean green, needs attention yellow, unknown gray."""
    if not r['synced'] or (not r['upstream'] and not r['changed']):
        return ui.GRAY
    return ui.OK if r['state'] == 'clean' else ui.WARN


def cmd_status(ws, args):
    items = [('(shell)', ws.root)] + [(d, ws.domain_path(d)) for d in ws.domains()]
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(lambda it: repo_state(ws, it[0], it[1], not args.no_fetch), items))
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    ui.table(
        [
            [
                r['repo'],
                r['branch'] or '-',
                r['commit'],
                ui.paint(r['state'], state_color(r), stream=sys.stdout),
            ]
            for r in rows
        ],
        header=['REPO', 'BRANCH', 'COMMIT', 'STATE'],
    )


def package_dirs(ws, domains):
    """{package: (domain, dir)} by package.xml."""
    out = {}
    for d in domains:
        for dirpath, dirnames, filenames in os.walk(ws.domain_path(d)):
            dirnames[:] = [x for x in dirnames if not x.startswith('.')]
            if 'COLCON_IGNORE' in filenames:
                dirnames[:] = []
                continue
            if 'package.xml' in filenames:
                with open(os.path.join(dirpath, 'package.xml'), encoding='utf-8') as f:
                    m = re.search(r'<name>\s*([^<\s]+)\s*</name>', f.read())
                if m:
                    out[m.group(1)] = (d, dirpath)
                dirnames[:] = []
    return out


def cmd_list(ws, args):
    domains = pick_domains(ws, args.domains)
    rows = []
    for pkg, (d, path) in sorted(package_dirs(ws, domains).items(), key=lambda x: (x[1][0], x[0])):
        kind = 'cpp' if os.path.isfile(os.path.join(path, 'CMakeLists.txt')) else 'python'
        rows.append(
            {'domain': d, 'package': pkg, 'type': kind, 'path': os.path.relpath(path, ws.root)}
        )
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    # the domain column already says where: show the path inside domains/<domain>/
    ui.table(
        [[r['domain'], r['package'], r['type'], r['path'].split('/', 2)[-1]] for r in rows],
        header=['DOMAIN', 'PACKAGE', 'TYPE', 'PATH'],
    )
    if not rows:
        ui.info('no packages yet: ditx new <domain> <package>')


def cmd_plugins(ws, args):
    rows = [
        {
            'name': name,
            'summary': summary,
            'path': os.path.relpath(path, ws.root) if path.startswith(ws.root) else path,
        }
        for name, (path, summary) in sorted(ws.plugins().items())
    ]
    if args.json:
        print(json.dumps(rows, indent=1))
        return
    ui.table(  # the description last: it is what gets cut on a narrow terminal
        [[r['name'], r['path'], r['summary'] or '-'] for r in rows],
        header=['PLUGIN', 'FILE', 'WHAT IT DOES'],
    )
    if not rows:
        ui.info('none yet: put an executable called ditx-<name> into tools/ext/')


def cmd_robot(ws, args):
    """`git remote` for robots: robots.ini is edited line by line, so comments stay."""
    robots = ws.robots()
    if args.action == 'list':
        rows = [
            {
                'name': r.name,
                'host': r.host,
                'arch': r.arch,
                'domain_id': r.domain_id,
                'profiles': r.profiles,
                'tag': r.tag,
            }
            for r in robots.values()
        ]
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        ui.table(
            [
                [
                    r['name'],
                    r['host'],
                    r['arch'],
                    r['domain_id'],
                    ','.join(r['profiles']) or '-',
                    r['tag'],
                ]
                for r in rows
            ],
            header=['ROBOT', 'HOST', 'ARCH', 'DOMAIN ID', 'PROFILES', 'TAG'],
        )
        if not rows:
            ui.info('no robots yet: ditx robot add <name> --host DIT-2026-11')
        return 0
    name = args.name
    if not name:
        ui.error(f'ditx robot {args.action}: which robot? e.g. ditx robot {args.action} white')
        return 2
    rel = os.path.relpath(ws.robots_path(), ws.root)
    if args.action == 'add':
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            ui.error(f'robot name {name!r}: use letters, digits, - and _ only')
            return 2
        if name in robots:
            ui.error(f'robot {name} already exists in {rel}  → ditx robot remove {name} first')
            return 1
        if not args.host:
            ui.error(
                f'ditx robot add {name}: --host is required: the robot hostname, e.g. DIT-2026-11'
            )
            return 2
        host, host_domain = resolve_host(args.host, args.user)
        domain_id = (
            args.domain_id or host_domain
        )  # DIT-Scripts sets ROS_DOMAIN_ID to the hostname number
        if not domain_id:  # the next free ROS_DOMAIN_ID, so robots never hear each other
            used = [int(r.domain_id) for r in robots.values() if r.domain_id.isdigit()]
            domain_id = str(max([9, *[u for u in used if u < 42]]) + 1)
        elif not domain_id.isdigit() or int(domain_id) > 232:
            ui.error(f'--domain-id {domain_id}: a number from 0 to 232')
            return 2
        elif host_domain and domain_id != host_domain:
            ui.warn(
                f'domain id {domain_id} differs from {host_domain}, the ROS_DOMAIN_ID DIT-Scripts '
                f'gives {host}: the dashboard and robot_status_bridge stay on {host_domain}'
            )
        clash = [r.name for r in robots.values() if r.domain_id == domain_id]
        if clash:
            ui.warn(f'domain id {domain_id} is also used by {", ".join(clash)}')
        fields = {
            'host': host,
            'arch': args.arch or 'amd64',
            'domain_id': domain_id,
            'profiles': args.profiles or ','.join(ws.domains()),
            'tag': args.tag or 'stable',
        }
        with ui.Task(f'add {name}') as task:
            if not args.dry_run:
                ws.add_robot(name, fields)
            task.done(f'{fields["host"]} · domain id {domain_id} → {rel}')
        if not args.dry_run:
            ui.info(f'next: ditx doctor -r {name}, then ditx deploy -r {name}')
        return 0
    if name not in robots:
        known = ', '.join(robots) or 'none'
        ui.error(f'no robot {name} in {rel} (robots: {known})')
        return 1
    if not (args.dry_run or args.yes or os.environ.get('DITX_CONFIRMED') == '1'):
        if not sys.stdin.isatty():
            ui.error(f'ditx robot remove {name}: add --yes to confirm')
            return 2
        print(
            f'{ui.paint("?", ui.WARN, bold=True)} remove robot {name} from {rel}? [y/N] ',
            end='',
            file=sys.stderr,
            flush=True,
        )
        try:
            if input().strip().lower() not in ('y', 'yes'):
                ui.info('kept')
                return 1
        except EOFError:
            return 1
    with ui.Task(f'remove {name}') as task:
        if not args.dry_run:
            ws.remove_robot(name)
        task.done(f'{robots[name].host} removed from {rel}')
    return 0


def cmd_cd(ws, args):
    """Print the directory; the `ditx` shell function (./install.sh) cds into it."""
    target = args.target or ''
    if not target:
        path = ws.root
    elif target in ws.domains():
        path = ws.domain_path(target)
    else:
        found = package_dirs(ws, ws.synced_domains()).get(target)
        if not found:
            raise Fail(f'no domain or package called {target!r} (see: ditx list)')
        path = found[1]
    print(path)
    if not os.environ.get('DITX_SHELL_FUNCTION'):
        ui.info(
            '(a program cannot change your shell directory: install the ditx shell '
            'function with ./install.sh, or use: cd "$(ditx cd ...)")'
        )


def ci_tags():
    """Image tags for the current CI ref: develop → develop, main → stable, vX.Y.Z → vX.Y.Z."""
    ref = os.environ.get('GITHUB_REF_NAME') or os.environ.get('CI_COMMIT_REF_NAME') or ''
    is_tag = os.environ.get('GITHUB_REF_TYPE') == 'tag' or bool(os.environ.get('CI_COMMIT_TAG'))
    sha = (os.environ.get('GITHUB_SHA') or os.environ.get('CI_COMMIT_SHA') or '')[:7]
    if is_tag and re.fullmatch(r'v\d+\.\d+\.\d+', ref):
        return ref, f'sha-{sha}'
    return {'develop': ('develop', f'sha-{sha}'), 'main': ('stable', f'sha-{sha}')}.get(
        ref, (None, None)
    )


BAKE_STEP = re.compile(r'^#\d+ \[([\w.-]+)(?: ([\w.-]+))? (\d+/\d+)\] (.*)')


def cmd_image(ws, args):
    domains = ws.synced_domains()
    if args.target in ('default', 'dev', 'cross', 'runtime') and not domains:
        raise Fail('dev/runtime images need the domains (run: ditx sync)')
    env = {
        **os.environ,
        'REGISTRY': ws.registry,
        'TAG': args.tag,
        'ROS_DISTRO': ws.distro,
        'PLATFORMS': args.platforms,
        'DOMAINS': ' '.join(domains),
        'EXTRA_TAGS': getattr(args, 'extra_tags', '') or '',
    }
    multi = ',' in args.platforms
    if multi and not args.push:
        raise Fail(
            'multi-platform images can only be pushed (--push), not loaded into the local docker'
        )
    os.makedirs(os.path.join(ws.root, 'domains'), exist_ok=True)
    cmd = ['docker', 'buildx', 'bake', '-f', 'docker/docker-bake.hcl', '--progress=plain']
    cmd.append('--push' if args.push else '--load')
    cache = os.environ.get('DIT_BAKE_CACHE')  # set by CI: type=gha or type=registry
    if cache:
        cmd += ['--set', f'*.cache-from={cache}', '--set', f'*.cache-to={cache},mode=max']
    for extra in os.environ.get('DIT_BAKE_SET', '').split():  # e.g. *.network=host
        cmd += ['--set', extra]
        if extra.endswith('network=host'):
            cmd += ['--allow', 'network.host']
    cmd.append(args.target)
    tags = ' + '.join([args.tag] + (getattr(args, 'extra_tags', '') or '').split())
    ui.step(f'images  {ws.distro} · {args.platforms} → {ws.registry}/eurobot-*:{tags}')
    if args.dry_run:
        run(cmd, args, cwd=ws.root, env=env)
        ui.info('dry run: nothing built' + (' or pushed' if args.push else ''))
        return
    names, steps = [], set()

    def on_line(line):
        m = BAKE_STEP.match(line)
        if m:
            target, stage, n, what = m.groups()
            steps.add(line.split()[0])
            what = what.replace('RUN ', '').split(' && ')[0][:50]
            task.status(f'{target} {stage or ""} {n}  {what}'.replace('  ', ' '))
        m = re.search(r'naming to (\S+)', line)
        if m:
            names.append(m.group(1).rsplit('/', 1)[-1].replace('eurobot-', '').split(':')[0])

    with ui.Task(args.target if args.target != 'default' else 'images') as task:
        r = run(cmd, args, check=False, cwd=ws.root, env=env, on_line=on_line)
        if r.returncode:
            task.fail('build failed')
            errors = [x for x in r.stdout.splitlines() if 'ERROR' in x or 'error' in x]
            ui.show_tail(errors[-15:] or r.stdout.splitlines())
            raise Fail('image build failed')
        built = ', '.join(dict.fromkeys(names)) or args.target
        task.status(f'{built}  ({"pushed" if args.push else "loaded"}, {len(steps)} steps)')


def cmd_clean(ws, args):
    if args.all:
        targets = [os.path.join(ws.root, 'build')]
    else:
        names = (pick_domains(ws, args.domains) if args.domains or not args.base else []) + (
            ['base'] if args.base else []
        )
        targets = [ws.build_dir(a, n) for a in ('amd64', 'arm64') for n in names]
    removed = []
    for p in targets:
        if os.path.exists(p):
            ui.command(f'rm -rf {os.path.relpath(p, ws.root)}', force=args.dry_run)
            if not args.dry_run:
                # files written by the container may be owned by another uid
                shutil.rmtree(p, ignore_errors=True)
            removed.append(os.path.relpath(p, ws.root))
    with ui.Task('clean') as t:
        t.status(
            ('would remove ' if args.dry_run else 'removed ') + ', '.join(removed)
            if removed
            else 'nothing to remove'
        )


# keep in sync with .github/workflows/docs.yml
DOCS_IMAGE = 'squidfunk/mkdocs-material:9.8.1'


def cmd_docs(ws, args):
    if not re.fullmatch(r'\d{1,5}', args.port or ''):
        raise Fail(f'--port must be a number, got {args.port!r}')
    site_path = '/'
    with open(os.path.join(ws.root, 'mkdocs.yml'), encoding='utf-8') as f:
        found = re.search(r'(?m)^site_url:\s*\S+?://[^/\s]+(/\S*)', f.read())
        if found:
            site_path = found.group(1).rstrip('/') + '/'
    ensure_image(DOCS_IMAGE, args, label='docs image')
    # no --init: the image already runs tini as PID 1 (two inits make tini warn)
    cmd = ['docker', 'run', '--rm', '-u', f'{os.getuid()}:{os.getgid()}']
    cmd += ['-e', 'NO_MKDOCS_2_WARNING=1', '-v', f'{ws.root}:/docs']
    if args.action == 'build':
        cmd += [DOCS_IMAGE, 'build', '--strict']
        with ui.Task('docs') as t:
            t.status('building (strict)')
            r = run(cmd, args, check=False)
            if r.returncode:
                ui.show_tail(r.stdout.splitlines())
                t.fail('build failed (warnings count as errors)')
                return r.returncode
            t.done('would build site/' if args.dry_run else 'site/  strict, no warnings')
        return 0
    cmd[3:3] = ['-it' if sys.stdin.isatty() else '-i', '--name', f'ditx-docs-{args.port}']
    cmd += ['-p', f'127.0.0.1:{args.port}:8000', DOCS_IMAGE, 'serve', '-a', '0.0.0.0:8000']
    ui.step(f'docs → http://localhost:{args.port}{site_path}  · reloads on save · Ctrl+C stops')
    ui.command(shlex.join(cmd), force=args.dry_run)
    if args.dry_run:
        return 0
    os.execvp(cmd[0], cmd)


def ufw_enabled():
    try:
        with open('/etc/ufw/ufw.conf', encoding='utf-8') as f:
            return any(line.strip() == 'ENABLED=yes' for line in f)
    except OSError:
        return False


def ufw_hint(robot):
    """DDS sends to ports that are not replies (discovery multicast, per-participant unicast),
    so a deny-incoming firewall drops robot -> laptop traffic; loopback (the sim) is unaffected."""
    host = robot.host.rpartition('@')[2]
    try:
        host = socket.gethostbyname(host)
    except OSError:
        pass
    return f"sudo ufw allow from {host} proto udp comment 'ditx {robot.name} DDS'"


def cmd_doctor(ws, args):
    if args.robot:
        get_robot(ws, args.robot)  # unknown robot: an error, not silently skipped
    if not args.json:
        ui.banner('DIT-X · Eurobot workspace · doctor')
    bad = 0
    results = []

    def check(label, good, fix='', warn_only=False):
        nonlocal bad
        level = 'ok' if good else 'warn' if warn_only else 'error'
        results.append({'check': label, 'level': level, 'fix': '' if good else fix})
        bad += level == 'error'
        if args.json:
            return
        if good:
            ui.ok(label)
        elif warn_only:
            ui.warn(f'{label}  → {fix}')
        else:
            ui.error(f'{label}  → {fix}')

    def ok_cmd(*cmd):
        return subprocess.run(cmd, capture_output=True).returncode == 0

    check('docker', ok_cmd('docker', 'info'), 'install Docker and add yourself to the docker group')
    check(
        'docker compose v2',
        ok_cmd('docker', 'compose', 'version'),
        'apt install docker-compose-plugin',
    )
    check(
        'docker buildx', ok_cmd('docker', 'buildx', 'version'), 'apt install docker-buildx-plugin'
    )
    check('git', ok_cmd('git', '--version'), 'apt install git')
    check(
        'arm64 emulation (qemu binfmt)',
        os.path.exists('/proc/sys/fs/binfmt_misc/qemu-aarch64'),
        'optional, only `ditx shell --arch arm64` needs it:\n'
        '      docker run --privileged --rm tonistiigi/binfmt --install arm64',
        warn_only=True,
    )
    mine = os.path.realpath(os.path.join(ws.root, 'bin', 'ditx'))
    found = shutil.which('ditx')
    check(
        'ditx on PATH',
        bool(found) and os.path.realpath(found) == mine,
        'run ./install.sh' if not found else f'{found} is another checkout: run ./install.sh here',
        warn_only=True,
    )
    home = os.path.expanduser('~')
    rcs = [os.path.join(home, f) for f in ('.bashrc', '.zshrc')]

    def has_marker(path):
        try:
            with open(path, encoding='utf-8') as f:
                return '# >>> ditx (DIT-X) >>>' in f.read()
        except OSError:
            return False

    check(
        'shell completion installed',
        any(map(has_marker, rcs))
        or os.path.exists(os.path.join(home, '.config/fish/conf.d/ditx.fish')),
        'run ./install.sh',
        warn_only=True,
    )
    du = shutil.disk_usage(ws.root)
    if du.free < 8e9:
        need = 'free up space: images + builds need ~20 GB'
        check(f'disk: {du.free / 1e9:.0f} GB free', False, need)
    else:
        check(
            f'disk: {du.free / 1e9:.0f} GB free',
            du.free > 20e9,
            'a bit tight: images + builds want ~20 GB',
            warn_only=True,
        )
    workers, mj, mem = auto_jobs()
    if not args.json:
        ui.info(f'build budget: {workers} colcon workers x make -j{mj}, container RAM cap {mem} GB')
    img = ws.dev_image()
    local = img.endswith(':local')
    have = ok_cmd('docker', 'image', 'inspect', img)
    check(
        f'dev image {img}' + ('' if have or local else ' (not pulled yet)'),
        have,
        'ditx image --target dev'
        if local
        else 'ditx build pulls it the first time (or: docker pull)',
        warn_only=not local,  # a published image is pulled on demand
    )
    check(
        f'domains synced: {", ".join(ws.synced_domains()) or "none"}',
        bool(ws.synced_domains()),
        'ditx sync',
    )
    robots = [r for n, r in ws.robots().items() if not r.is_local and args.robot in (None, n)]

    def reach(r):
        return ok_cmd('ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2', r.host, 'docker info')

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for r, up in zip(robots, ex.map(reach, robots), strict=True):
            # robots are often off or on another network: a warning, not a failure
            check(
                f'robot {r.name} ({r.host}) ' + ('reachable' if up else 'not reachable'),
                up,
                'off, or on another network?',
                warn_only=not args.robot,
            )
            env_id = robot_env_domain_id(r.host) if up else None
            if env_id:
                # robots.ini may override it, but the host services DIT-Scripts starts
                # (dashboard, robot_status_bridge) stay on the system ROS_DOMAIN_ID
                check(
                    f'robot {r.name} domain id {r.domain_id}'
                    + (' = the robot' if env_id == r.domain_id else f', the robot says {env_id}'),
                    env_id == r.domain_id,
                    f'set domain_id = {env_id} for [{r.name}] in config/robots.ini, unless '
                    f'the stack must not see the dashboard',
                    warn_only=True,
                )
            if up and ufw_enabled() and not args.json:
                hint = ufw_hint(r)
                ui.info(f'ufw is on: if {r.name} topics are missing here, allow its DDS: {hint}')
    if args.json:
        print(
            json.dumps(
                {
                    'ok': not bad,
                    'checks': results,
                    'build_budget': {'workers': workers, 'jobs': mj, 'ram_gb': mem},
                },
                indent=1,
            )
        )
    return 1 if bad else 0


def cmd_ci(ws, args):
    """Exactly what .github/workflows and .gitlab-ci.yml run."""
    ns = dict(dry_run=args.dry_run, domains=args.domains)
    if args.stage == 'lint':
        return cmd_lint(ws, argparse.Namespace(**ns, fix=False))
    if args.stage in ('build', 'cross'):
        return cmd_build(
            ws,
            argparse.Namespace(
                **ns,
                packages=[],
                base=False,
                arch='arm64' if args.stage == 'cross' else None,
                jobs='auto',
                workers='auto',
                clangd=False,
                build_type='RelWithDebInfo',
                clean=False,
            ),
        )
    if args.stage == 'test':
        return cmd_test(ws, argparse.Namespace(**ns, packages=[], base=True, integration=True))
    if args.stage == 'image':
        tag, extra = ci_tags()
        push = tag is not None
        tag = tag or 'ci'
        ui.info(f'ref → tag {tag}{" (push)" if push else " (build only, not a release branch)"}')
        return cmd_image(
            ws,
            argparse.Namespace(
                dry_run=args.dry_run,
                target='default',
                tag=tag,
                platforms=os.environ.get('DIT_PLATFORMS', 'linux/amd64'),
                push=push,
                extra_tags=extra or '',
            ),
        )


def cmd_init(ws, args):
    """Ask for the local settings and update .env in place (other lines are kept)."""
    if not sys.stdin.isatty() and not args.defaults:
        raise Fail('init asks questions: run it in a terminal, or pass --defaults')
    ui.banner('DIT-X · Eurobot workspace · init')
    path = os.path.join(ws.root, '.env')
    cur = {**ws.env}
    teams = ws.domains() or ['main']
    # (key, question, allowed answers, default); the team is also your login on the robots
    questions = [
        ('DIT_ROBOT', 'Default robot', list(ws.robots()), cur.get('DIT_ROBOT', 'sim')),
        ('DIT_ROBOT_USER', 'Your team', teams, cur.get('DIT_ROBOT_USER', 'main')),
        ('ROS_DISTRO', 'ROS distro', ['humble', 'jazzy'], cur.get('ROS_DISTRO', 'humble')),
    ]
    want = {}
    for key, prompt, choices, default in questions:
        if choices and default not in choices:  # a stale .env value: offer a valid one
            default = choices[0]
        while True:
            val = '' if args.defaults else input(f'{prompt} ({"/".join(choices)}) [{default}]: ')
            val = val.strip() or default
            if args.defaults or val in choices:
                break
            ui.warn(f'{val!r} is not one of: {", ".join(choices)}')
        want[key] = val
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        lines = ['# local settings for ditx; not committed (see .env.example)']
    changed = []
    for key, val in want.items():
        idx = next((i for i, x in enumerate(lines) if re.match(rf'\s*{key}\s*=', x)), None)
        if idx is None:
            lines.append(f'{key}={val}')
            changed.append(key)
        elif lines[idx].split('=', 1)[1].strip().strip('"\'') != val:
            lines[idx] = f'{key}={val}'
            changed.append(key)
    if not changed:
        ui.ok('.env already up to date')
        return
    if args.dry_run:
        ui.info(f'would set {", ".join(f"{k}={want[k]}" for k in changed)} in .env')
        return
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    ui.ok(f'.env: set {", ".join(changed)}; next: ditx sync && ditx image && ditx build')


def cmd_tui(ws, args):
    flags = {f for f in ('dry_run', 'verbose') if getattr(args, f, False)}
    return open_tui(ws, flags)


def cmd_completion(ws, args):
    with open(
        os.path.join(ws.root, 'tools', 'completion', f'ditx.{args.shell}'), encoding='utf-8'
    ) as f:
        sys.stdout.write(f.read())


if __name__ == '__main__':
    sys.exit(main())
