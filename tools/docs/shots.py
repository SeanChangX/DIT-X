#!/usr/bin/env python3
"""Terminal screenshots for the docs: run real ditx commands, render them to SVG.

    python3 tools/docs/shots.py                 # regenerate every deterministic shot
    python3 tools/docs/shots.py --check         # CI: exit 1 if an image is stale
    python3 tools/docs/shots.py help-build      # only these ids
    python3 tools/docs/shots.py --list          # every shot, with its kind and docs
    python3 tools/docs/shots.py --map           # rewrite docs/assets/term/MAP.md

Stateful shots (sync, build, deploy, the sim robot, ...) need a real workspace with
synced domains and docker. They run in order, as one scripted tutorial, in a
disposable sandbox that this script prepares and removes:

    python3 tools/docs/shots.py --prepare --sandbox ../_shots \\
        --repos-base file:///path/to/local/domain/repos     # optional: local clones
    python3 tools/docs/shots.py --stateful --sandbox ../_shots
    python3 tools/docs/shots.py --teardown --sandbox ../_shots

or all three in one go (the sandbox is removed even when a shot fails):

    python3 tools/docs/shots.py --fresh --sandbox ../_shots [--repos-base file:///...]

The sandbox runs its sim robot as compose project `dit-shots` on ROS_DOMAIN_ID 77,
so it never touches a sim you are running yourself (project eurobot-sim, domain 42).

Images go to docs/assets/term/<id>.svg. Every command runs with FORCE_COLOR=1, a fixed
COLUMNS, a clean environment (no CI variables, git user `student`) and with
machine-specific values replaced (workspace path -> ~/DIT-X, home -> ~, host name).
Deterministic shots run in a fresh copy of this working tree each, so they never
change it. Standard library only.
"""

import argparse
import dataclasses
import difflib
import getpass
import glob
import html
import os
import re
import select
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT_DIR = os.path.join(ROOT, 'docs', 'assets', 'term')
sys.path.insert(0, HERE)

import termshot  # noqa: E402

SHOWN_ROOT = '~/DIT-X'  # what the workspace path becomes in the images
SHOT_PROJECT = 'dit-shots'  # compose project of the sandbox sim robot
SHOT_DOMAIN_ID = '77'  # ROS_DOMAIN_ID of the sandbox sim robot (the real config says 42)


@dataclasses.dataclass
class Shot:
    id: str
    cmd: str  # run with bash -c (`ditx` = bin/ditx of the workspace) and shown after the prompt
    cols: int = 100
    stateful: bool = False  # needs the prepared sandbox (synced domains, docker, sim robot)
    state: str = ''  # what the workspace must look like (for humans and --list)
    setup: str = ''  # bash run in the workspace first; not shown, output discarded
    tty: bool = False  # run in a pseudo terminal (prompts); spinners then animate too
    answer: str = ''  # typed into the tty once its output contains `expect`
    expect: str = '[y/N]'
    needs: tuple = ()  # 'dev-image': skipped (not failed) when the dev image is missing
    subs: tuple = ()  # extra (regex, replacement) pairs for this shot
    docs: tuple = ()  # (doc file, first line of the ```text block it replaces)
    title: str = SHOWN_ROOT
    timeout: int = 900
    show: str = ''  # shown instead of cmd (when the real call needs extra arguments)
    keys: tuple = ()  # full-screen TUI: tmux keys sent in order, then the screen is captured
    rows: int = 30  # terminal height for `keys` shots


def help_shot(name, first_line, cols=100):
    return Shot(
        f'help-{name}',
        f'ditx help {name}',
        cols=cols,
        state='any checkout',
        docs=(('reference/commands.md', first_line),),
    )


# The pkgcount plugin written in tutorial 13 (same file as in the docs).
PKGCOUNT = r"""cat > tools/ext/ditx-pkgcount <<'EOF'
#!/bin/bash
# ditx-plugin: count ROS packages per domain
set -euo pipefail
cd "$DITX_ROOT"
for d in domains/*/; do
  n=$(find "$d" -name package.xml -not -path '*/build/*' | wc -l)
  printf '%-14s %s packages\n' "$(basename "$d")" "$n"
done
EOF
chmod +x tools/ext/ditx-pkgcount
"""

# ── tutorial 04/05: vision_hello ────────────────────────────────────────────

HELLO_FILES = r"""P=domains/vision/src/vision_hello
cat > $P/vision_hello/hello.py <<'EOF'
"""
HELLO_FILES += '''"""Pure logic of vision_hello: no ROS here, so it is easy to unit-test."""


def greeting(count):
    """Text of the count-th hello message."""
    return f'hello #{count} from vision'
EOF
rm -f $P/test/test_*.py
cat > $P/test/test_hello.py <<'EOF'
from vision_hello.hello import greeting


def test_greeting_counts():
    assert greeting(0) == 'hello #0 from vision'
    assert greeting(42).startswith('hello #42')
EOF
cat > $P/vision_hello/hello_node.py <<'EOF'
"""hello_node of the vision domain: publishes a greeting on /vision/hello."""

from eurobot_common.startup import StartupReporter
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from vision_hello.hello import greeting


class HelloNode(Node):
    def __init__(self):
        super().__init__('hello')
        rate_hz = self.declare_parameter('rate_hz', 1.0).value
        self.pub = self.create_publisher(String, '/vision/hello', 10)
        self.count = 0
        self.create_timer(1.0 / rate_hz, self.tick)
        self.reporter = StartupReporter(self, 'vision', 'hello')
        self.reporter.ready()

    def tick(self):
        self.pub.publish(String(data=greeting(self.count)))
        self.count += 1


def main():
    rclpy.init()
    node = HelloNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
EOF
grep -q std_msgs $P/package.xml || \\
  sed -i 's|  <exec_depend>rclpy</exec_depend>|&\\n  <exec_depend>std_msgs</exec_depend>|' \\
    $P/package.xml
B=domains/vision/src/vision_bringup
python3 - <<'EOF'
p = 'domains/vision/src/vision_bringup/launch/bringup.launch.py'
s = open(p).read()
node = """        Node(
            package='vision_hello',
            executable='hello_node',
            name='hello',
            parameters=params,
            output='screen',
            emulate_tty=True,
        ),
    ]
"""
if 'vision_hello' not in s:
    s = s.replace('        ),\\n    ]\\n', '        ),\\n' + node, 1)
    open(p, 'w').write(s)
EOF
grep -q vision_hello $B/package.xml || \\
  sed -i \\
    's|  <exec_depend>vision_rival</exec_depend>|&\\n  <exec_depend>vision_hello</exec_depend>|' \\
    $B/package.xml
grep -q '^hello:' $B/config/default.yaml || \\
  printf 'hello:\\n  ros__parameters:\\n    rate_hz: 1.0\\n' >> $B/config/default.yaml
'''

HELLO_CONTRACT = r"""python3 - <<'EOF'
p = 'base/eurobot_common/contract.yaml'
s = open(p).read()
entry = '''  VISION_HELLO:
    name: /robot/vision/hello
    type: std_msgs/msg/String
    qos: sensor
    publisher: vision
    subscribers: [main]
    rate_hz: 1
    doc: Tutorial example, a greeting from the vision domain.

'''
if 'VISION_HELLO' not in s:
    s = s.replace('topics:\n', 'topics:\n' + entry, 1)
    open(p, 'w').write(s)
p = 'domains/vision/src/vision_hello/vision_hello/hello_node.py'
s = open(p).read()
s = s.replace('from eurobot_common.startup',
              'from eurobot_common import contract\nfrom eurobot_common.startup')
s = s.replace("self.pub = self.create_publisher(String, '/vision/hello', 10)",
              'topic = contract.VISION_HELLO\n        '
              'self.pub = self.create_publisher(String, topic.name, topic.qos())')
open(p, 'w').write(s)
EOF
"""

# back to the plain develop state of the shell repo and of every domain
RESET_ALL = r"""git checkout -q -- base 2>/dev/null || true
for d in domains/*/; do
  git -C "$d" checkout -q -- . && git -C "$d" clean -qfd && git -C "$d" switch -q develop
done
"""

ARM_REPO = r"""A=$(cd .. && pwd)/Eurobot-Arm
if [ ! -d "$A" ]; then
  git init -q -b develop "$A"
  cp domains/vision/.gitignore domains/vision/CODEOWNERS "$A"/ 2>/dev/null || true
  printf '# Eurobot-Arm\n\nThe arm domain of the DIT Eurobot robot.\n' > "$A/README.md"
  mkdir -p "$A/src" && touch "$A/src/.gitkeep"
  git -C "$A" add -A && git -C "$A" commit -qm "chore: start the arm domain"
fi
ENTRY='  domains/arm:\n    type: git\n    url: file://%s\n    version: develop\n'
grep -q 'domains/arm:' ditx.repos || printf "$ENTRY" "$A" >> ditx.repos
"""

ARM_BRINGUP = r"""cp -r domains/vision/src/vision_bringup domains/arm/src/arm_bringup
cd domains/arm/src/arm_bringup
mv vision_bringup arm_bringup
mv resource/vision_bringup resource/arm_bringup
grep -rl -e vision -e rival . | xargs sed -i \
  's/vision_rival/arm_gripper/g; s/rival_node/gripper_node/g; s/vision/arm/g; s/rival/gripper/g'
printf 'gripper:\n  ros__parameters:\n    rate_hz: 10.0\n' > config/default.yaml
"""

# The copies deterministic shots run in have no .git; the TUI header shows the branch.
GIT_BRANCH = 'git init -q -b develop && git commit -q --allow-empty -m "chore: shot"'

SHOTS = [
    # ── deterministic: help texts ───────────────────────────────────────────
    Shot(
        'help',
        'ditx --help',
        state='any checkout',
        docs=(
            (
                'reference/commands.md',
                'ditx (DIT-X): build, test and deploy the Eurobot workspace.',
            ),
        ),
    ),
    help_shot('build', 'usage: ditx build [flags]'),
    help_shot('test', 'usage: ditx test [flags]'),
    help_shot('lint', 'usage: ditx lint [flags]'),
    help_shot('index', 'usage: ditx index [flags]'),
    help_shot('shell', 'usage: ditx shell [flags]'),
    help_shot('new', 'usage: ditx new DOMAIN PACKAGE [flags]'),
    help_shot('deploy', 'usage: ditx deploy [flags]'),
    help_shot('rollback', 'usage: ditx rollback [flags]'),
    help_shot('overlays', 'usage: ditx overlays [flags]'),
    help_shot('up', 'usage: ditx up [flags]'),
    help_shot('down', 'usage: ditx down [flags]'),
    help_shot('logs', 'usage: ditx logs [flags]'),
    help_shot('contract', 'usage: ditx contract [ACTION] [flags]'),
    help_shot('robot', 'usage: ditx robot [ACTION] [NAME] [flags]'),
    help_shot('sync', 'usage: ditx sync [DOMAINS] [flags]'),
    help_shot('list', 'usage: ditx list [flags]'),
    help_shot('plugins', 'usage: ditx plugins [flags]'),
    help_shot('cd', 'usage: ditx cd [TARGET] [flags]'),
    help_shot('status', 'usage: ditx status [flags]'),
    help_shot('image', 'usage: ditx image [flags]'),
    help_shot('clean', 'usage: ditx clean [flags]'),
    help_shot('doctor', 'usage: ditx doctor [flags]'),
    help_shot('docs', 'usage: ditx docs [ACTION] [flags]'),
    help_shot('ci', 'usage: ditx ci STAGE [flags]'),
    help_shot('init', 'usage: ditx init [flags]'),
    help_shot('tui', 'usage: ditx tui [flags]'),
    help_shot('completion', 'usage: ditx completion SHELL [flags]'),
    # ── deterministic: commands that need no domains ────────────────────────
    Shot(
        'plugins',
        'ditx plugins',
        state='any checkout',
        docs=(('tutorials/advanced/13-plugins.md', 'PLUGIN  WHAT IT DOES'),),
    ),
    Shot(
        'plugins-pkgcount',
        'ditx plugins',
        state='tools/ext/ditx-pkgcount from tutorial 13',
        setup=PKGCOUNT,
        docs=(('tutorials/advanced/13-plugins.md', 'PLUGIN    WHAT IT DOES'),),
    ),
    Shot(
        'did-you-mean',
        'ditx pkgcont',
        state='tools/ext/ditx-pkgcount from tutorial 13',
        setup=PKGCOUNT,
        docs=(('tutorials/advanced/13-plugins.md', '$ ditx pkgcont'),),
    ),
    Shot('did-you-mean-build', 'ditx biuld', state='any checkout'),
    Shot(
        'tui',
        'ditx',
        keys=('Down',) * 7,  # → rollback (shows the ! confirm marker)
        setup=GIT_BRANCH,
        rows=24,
        state='any checkout, fresh ~/.config/ditx',
        docs=(('tutorials/beginner/03-tui.md', '█▀▄ ▀█▀ ▀█▀'),),
    ),
    Shot(
        'tui-build-dry-run',
        'ditx',
        keys=(
            'Tab',
            'Enter',
            'text:vis',
            'Space',
            'Enter',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Space',
            'n',
        ),
        setup=GIT_BRANCH,
        state='any checkout, fresh ~/.config/ditx',
    ),
    Shot(
        'robot',
        'ditx robot',
        state='config/robots.ini as committed',
        docs=(('tutorials/intermediate/10-robots.md', 'ROBOT  HOST   ARCH   DOMAIN ID'),),
    ),
    Shot(
        'robot-add',
        'ditx robot add white --host DIT-2026-11',
        state='config/robots.ini as committed',
        docs=(('tutorials/intermediate/10-robots.md', '✓ add white      main@dit-2026-11.local'),),
    ),
    Shot(
        'robot-add-exists',
        'ditx robot add white --host DIT-2026-11',
        state='robot white already added',
        setup='ditx robot add white --host DIT-2026-11',
        docs=(('tutorials/intermediate/10-robots.md', '✗ robot white already exists'),),
    ),
    Shot(
        'robot-remove',
        'ditx robot remove white --yes',
        state='robot white added',
        setup='ditx robot add white --host DIT-2026-11',
        docs=(('tutorials/intermediate/10-robots.md', '✓ remove white   main@dit-2026-11.local'),),
    ),
    Shot(
        'contract-validate',
        'ditx contract validate',
        state='any checkout; the dev image',
        needs=('dev-image',),
        docs=(('tutorials/beginner/05-contract.md', '✓ contract       valid'),),
    ),
    Shot('lint-dry-run', 'ditx -n lint', state='any checkout'),
    Shot('up-dry-run', 'ditx -n up -r sim', cols=120, state='any checkout'),
    Shot(
        'rollback-dry-run',
        'ditx -n rollback -d localization -r sim',
        cols=110,
        state='any checkout',
    ),
    Shot('clean-dry-run', 'ditx -n clean --all', state='any checkout'),
    Shot(
        'app-foxglove-dry-run',
        'ditx -n app foxglove -r sim',
        cols=120,
        state='any checkout; the dev image (the interfaces are copied out of it)',
        needs=('dev-image',),
        docs=(('tutorials/beginner/06-sim-robot.md', '▸ foxglove → robot sim'),),
    ),
    Shot(
        'commit-msg-hook',
        'git commit -am "update readme"',
        state='a git repo with tools/hooks/commit-msg installed',
        setup=(
            'git init -q -b develop . && cp tools/hooks/commit-msg .git/hooks/ && '
            'git add -A && git -c core.hooksPath=/dev/null commit -qm "chore: init" && '
            'echo >> README.md'
        ),
        docs=(('tutorials/intermediate/09-git-ci.md', '✗ commit message does not follow'),),
    ),
    # ── stateful: one scripted walk through the tutorials, in this order ────
    Shot(
        'doctor',
        'ditx doctor',
        cols=110,
        stateful=True,
        state='fresh clone, nothing synced',
        docs=(('tutorials/beginner/01-install.md', '____  __________   ____'),),
    ),
    Shot(
        'robot-install-dry-run',
        'infra/robot/install.sh --dry-run --user "$(id -un)"',
        show='sudo infra/robot/install.sh --dry-run',
        subs=((r'\bstudent\b', 'ditrobotics'),),
        cols=110,
        stateful=True,
        state='any machine (prints what it would do to it)',
        docs=(('tutorials/advanced/14-real-robot.md', '+ install -d -o ditrobotics -g docker'),),
    ),
    Shot(
        'sync',
        'ditx sync',
        stateful=True,
        state='fresh clone, domains not synced yet',
        docs=(('tutorials/beginner/02-first-build.md', '✓ (shell)        up to date'),),
    ),
    Shot(
        'tui-build-dry-run-result',
        'ditx',
        keys=(
            'Tab',
            'Enter',
            'text:vis',
            'Space',
            'Enter',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Down',
            'Space',
            'n',
            'r',
            'wait:4',
        ),
        stateful=True,
        state='synced domains (a dry-run build checks them), fresh ~/.config/ditx',
        subs=(
            (r'\d+ workers? × make -j\d+ · RAM cap \d+ GB', '3 workers × make -j5 · RAM cap 24 GB'),
            (r'RAM cap \d+ GB', 'RAM cap 24 GB'),
        ),
        docs=(('tutorials/beginner/03-tui.md', '✓  ⏎ back to the builder'),),
    ),
    Shot(
        'status',
        'ditx status',
        stateful=True,
        state='domains synced',
        docs=(('tutorials/beginner/02-first-build.md', 'REPO          BRANCH   COMMIT   STATE'),),
    ),
    Shot(
        'list',
        'ditx list',
        stateful=True,
        state='domains synced',
        docs=(('tutorials/beginner/02-first-build.md', 'REPO          BRANCH   COMMIT   STATE'),),
    ),
    Shot(
        'pkgcount',
        'ditx pkgcount',
        stateful=True,
        state='domains synced, tools/ext/ditx-pkgcount',
        setup=PKGCOUNT,
        docs=(('tutorials/advanced/13-plugins.md', 'localization   2 packages'),),
    ),
    Shot(
        'lint',
        'ditx lint',
        stateful=True,
        state='domains synced, dev image',
        setup='rm -f tools/ext/ditx-pkgcount',
        docs=(('tutorials/beginner/02-first-build.md', '✓ clang-format   13 files'),),
    ),
    Shot(
        'build',
        'ditx build',
        stateful=True,
        state='domains synced, dev image',
        docs=(('tutorials/beginner/02-first-build.md', '✓ clang-format   13 files'),),
    ),
    Shot(
        'test-integration',
        'ditx test --integration',
        stateful=True,
        state='everything built',
        docs=(('tutorials/beginner/02-first-build.md', '✓ clang-format   13 files'),),
    ),
    Shot(
        'up',
        'ditx up -r sim',
        stateful=True,
        state='runtime images :local',
        docs=(('tutorials/beginner/06-sim-robot.md', '✓ sim            up: main'),),
    ),
    Shot(
        'topics',
        'ditx topics -r sim',
        cols=110,
        stateful=True,
        state='sim robot up',
        setup='sleep 8',
        docs=(('tutorials/beginner/06-sim-robot.md', 'Published topics:'),),
    ),
    Shot(
        'contract-live',
        'ditx contract live -r sim',
        stateful=True,
        state='sim robot up',
        docs=(('tutorials/beginner/06-sim-robot.md', '✓ contract       8 topics checked'),),
    ),
    Shot(
        'logs-main',
        'ditx logs -r sim -d main | head -n 14',
        stateful=True,
        state='sim robot up',
        docs=(('tutorials/beginner/06-sim-robot.md', '[entrypoint] running stable image'),),
    ),
    Shot(
        'app-rviz-dry-run',
        'ditx -n app rviz -r sim',
        cols=120,
        stateful=True,
        state='sandbox robots.ini (domain id shown as 42)',
        subs=((rf'ROS_DOMAIN_ID[ =]{SHOT_DOMAIN_ID}\b', lambda m: m[0][:-2] + '42'),),
        docs=(('tutorials/beginner/06-sim-robot.md', '! workspace is '),),
    ),
    Shot(
        'deploy',
        'ditx deploy -d localization -r sim',
        stateful=True,
        state='sim robot up; localization on feat/try with the HELLO commit',
        setup=(
            'cd domains/localization && git switch -q -c feat/try && '
            "sed -i 's/^\\(\\s*\\)reporter_->ready();/\\1RCLCPP_INFO(get_logger(), "
            '"HELLO FROM MY OVERLAY");\\n&/\' src/localization_fusion/src/fusion_node.cpp && '
            'git commit -qam "feat(fusion): say hello"'
        ),
        docs=(('tutorials/intermediate/07-deploy.md', '▸ build  '),),
    ),
    Shot(
        'overlays',
        'ditx overlays -r sim',
        cols=110,
        stateful=True,
        state='an overlay deployed',
        docs=(('tutorials/intermediate/07-deploy.md', 'DOMAIN        OVERLAY (◉ active)'),),
    ),
    Shot(
        'logs-hello',
        'ditx logs -r sim -d localization | grep HELLO | tail -n 1',
        stateful=True,
        state='the HELLO overlay running',
        docs=(('tutorials/intermediate/07-deploy.md', 'DOMAIN        OVERLAY (◉ active)'),),
    ),
    Shot(
        'rollback',
        'ditx rollback -d localization -r sim',
        stateful=True,
        tty=True,
        answer='y\n',
        state='an overlay active',
        docs=(('tutorials/intermediate/07-deploy.md', '? ditx rollback restarts'),),
    ),
    Shot(
        'deploy-fail',
        'ditx deploy -d localization -r sim',
        cols=120,
        stateful=True,
        state='localization with an uncommitted change that throws at startup',
        setup=(
            'cd domains/localization && sed -i \'s/RCLCPP_INFO(get_logger(), "HELLO FROM MY '
            'OVERLAY");/throw std::runtime_error("boom");/\' '
            'src/localization_fusion/src/fusion_node.cpp'
        ),
        docs=(('tutorials/intermediate/07-deploy.md', '! localization has uncommitted changes'),),
    ),
    Shot(
        'overlays-failed',
        'ditx overlays -r sim',
        cols=110,
        stateful=True,
        state='a failed overlay',
        docs=(('tutorials/intermediate/07-deploy.md', 'localization    20261115-160121-student'),),
    ),
    Shot(
        'build-error',
        'ditx build -d localization',
        cols=140,
        stateful=True,
        state='a semicolon removed from fusion_node.cpp',
        setup=(
            'cd domains/localization && git checkout -q -- . && git switch -q develop && '
            "sed -i 's/reporter_->ready();/reporter_->ready()/' src/localization_fusion/src/"
            'fusion_node.cpp'
        ),
        docs=(('tutorials/intermediate/08-debugging.md', '▸ build  '),),
    ),
    Shot(
        'shell-cmd',
        """ditx shell --cmd 'ros2 pkg list | grep -E "^(eurobot|localization|vision)"'""",
        stateful=True,
        state='everything built',
        setup='git -C domains/localization checkout -q -- . && ditx build -d localization -q',
        docs=(('tutorials/intermediate/08-debugging.md', 'eurobot_common'),),
    ),
    Shot(
        'logs-fetch',
        'ditx logs -r sim --fetch',
        stateful=True,
        state='sim robot up',
        docs=(('tutorials/intermediate/08-debugging.md', '✓ fetch logs     sim → logs/sim'),),
    ),
    Shot(
        'new-python',
        'ditx new vision vision_hello --lang python',
        stateful=True,
        state='vision synced',
        docs=(('tutorials/beginner/04-first-package.md', '✓ vision_hello   python package'),),
    ),
    Shot(
        'lint-vision',
        'ditx lint -d vision',
        stateful=True,
        state='vision_hello written as in tutorial 04',
        setup=HELLO_FILES,
        docs=(('tutorials/beginner/04-first-package.md', '✓ clang-format   6 files'),),
    ),
    Shot(
        'build-vision',
        'ditx build -d vision',
        stateful=True,
        state='vision_hello written as in tutorial 04',
        docs=(('tutorials/beginner/04-first-package.md', '✓ clang-format   6 files'),),
    ),
    Shot(
        'test-vision',
        'ditx test -d vision',
        stateful=True,
        state='vision built',
        docs=(('tutorials/beginner/04-first-package.md', '✓ clang-format   6 files'),),
    ),
    Shot(
        'contract-validate-hello',
        'ditx contract validate',
        stateful=True,
        state='VISION_HELLO added to contract.yaml (tutorial 05)',
        setup=HELLO_CONTRACT,
    ),
    Shot(
        'build-base',
        'ditx build --base',
        stateful=True,
        state='contract.yaml changed',
        docs=(
            ('tutorials/beginner/05-contract.md', '✓ base           2 packages  22s'),
            ('tutorials/advanced/11-interfaces.md', '▸ build  '),
        ),
    ),
    Shot(
        'build-after-base',
        'ditx build',
        stateful=True,
        state='local base build',
        docs=(
            ('tutorials/beginner/05-contract.md', '✓ base           2 packages  22s'),
            ('tutorials/advanced/11-interfaces.md', '! localization: base changed'),
        ),
    ),
    Shot(
        'test-integration-hello',
        'ditx test --integration',
        stateful=True,
        state='everything built against the local base',
        docs=(('tutorials/beginner/05-contract.md', '✓ integration    handshake 1.0s · 9 topics'),),
    ),
    Shot(
        'deploy-skew',
        'ditx deploy -d localization -r sim',
        cols=160,
        stateful=True,
        state='local base build, sim robot up',
        docs=(('tutorials/advanced/11-interfaces.md', '▸ deploy → sim (this machine)'),),
    ),
    Shot(
        'deploy-allow-skew',
        'ditx deploy -d localization -r sim --allow-skew',
        cols=160,
        stateful=True,
        state='local base build, sim robot up',
        docs=(('tutorials/advanced/11-interfaces.md', '! localization: built against interfaces'),),
    ),
    Shot(
        'clean-base',
        'ditx clean --base --yes',
        stateful=True,
        state='local base build; overlay rolled back',
        setup='ditx rollback -d localization -r sim --yes',
        docs=(('tutorials/advanced/11-interfaces.md', '✓ clean          removed build/'),),
    ),
    Shot(
        'new-cpp',
        'ditx new localization localization_beacons --lang cpp',
        stateful=True,
        state='localization synced',
        setup=RESET_ALL + 'ditx build -q',
        docs=(('tutorials/advanced/15-cpp.md', '✓ localization_beacons cpp package'),),
    ),
    Shot(
        'build-clangd',
        'ditx build -d localization --clangd',
        stateful=True,
        state='localization_beacons scaffolded',
        docs=(('tutorials/advanced/15-cpp.md', '✓ localization   3 packages  10s'),),
    ),
    Shot(
        'test-localization',
        'ditx test -d localization',
        stateful=True,
        state='localization built',
        docs=(('tutorials/advanced/15-cpp.md', '✓ localization   10 tests'),),
    ),
    Shot(
        'build-arm64',
        'ditx build --arch arm64 -d localization',
        stateful=True,
        state='the eurobot-cross image is available',
        setup='ditx sync localization >/dev/null 2>&1',
        docs=(('tutorials/advanced/14-real-robot.md', '▸ build  humble/arm64'),),
    ),
    Shot(
        'sync-arm',
        'ditx sync',
        stateful=True,
        state='a new Eurobot-Arm repo added to ditx.repos (tutorial 12)',
        setup='rm -rf domains/localization/src/localization_beacons\n' + ARM_REPO,
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm            cloned'),),
    ),
    Shot(
        'new-arm',
        'ditx new arm arm_gripper --lang python',
        stateful=True,
        state='arm synced',
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm_gripper    python package'),),
    ),
    Shot(
        'build-arm',
        'ditx build -d arm',
        stateful=True,
        state='arm_bringup copied from vision_bringup',
        setup=ARM_BRINGUP,
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm            2 packages  2s'),),
    ),
    Shot(
        'test-arm',
        'ditx test -d arm',
        stateful=True,
        state='arm built',
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm            2 packages  2s'),),
    ),
    Shot(
        'lint-arm',
        'ditx lint -d arm',
        cols=120,
        stateful=True,
        state='arm built',
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm            2 packages  2s'),),
    ),
    Shot(
        'test-integration-arm-fail',
        'ditx test --integration 2>&1 | tail -n 4',
        cols=140,
        stateful=True,
        state="arm in main's required_domains, not in contract.yaml",
        setup=(
            "sed -i 's/required_domains: \\[main, navigation, localization, vision\\]/"
            "required_domains: [main, navigation, localization, vision, arm]/' "
            'domains/main/src/main_bringup/config/default.yaml'
        ),
        docs=(('tutorials/advanced/12-new-domain.md', '│ [gripper_node-1] [ERROR]'),),
    ),
    Shot(
        'test-integration-arm',
        'ditx test --integration',
        stateful=True,
        state='arm in contract.yaml domains, base and domains rebuilt',
        setup=(
            "sed -i 's/^domains: \\[main, navigation, localization, vision\\]/"
            "domains: [main, navigation, localization, vision, arm]/' "
            'base/eurobot_common/contract.yaml && ditx build --base -q && ditx build -q'
        ),
        timeout=1800,
        docs=(('tutorials/advanced/12-new-domain.md', '✓ arm            5 tests, 1 skipped'),),
    ),
    Shot(
        'doctor-unreachable',
        'ditx doctor -r white',
        cols=110,
        stateful=True,
        state='robot white added, not on this network',
        setup='ditx robot add white --host DIT-2026-11 >/dev/null 2>&1 || true',
        docs=(('tutorials/intermediate/10-robots.md', '✗ robot white (main@dit-2026-11.local)'),),
    ),
]

# ```text blocks in docs/ that are deliberately not shots (MAP.md lists them too)
NOT_SHOTS = [
    ('tutorials/advanced/15-cpp.md', 'localization_beacons/', 'file tree, not command output'),
    (
        'tutorials/beginner/02-first-build.md',
        '▸ images  ',
        'rebuilds the shared :local images',
    ),
    ('tutorials/beginner/04-first-package.md', 'domains/vision/src/vision_hello/', 'file tree'),
    ('tutorials/beginner/04-first-package.md', "data: 'hello #4 from vision'", 'ros2 output'),
    ('tutorials/intermediate/09-git-ci.md', 'feat/<topic> ──PR──▶ develop', 'diagram'),
]

# ── running ──────────────────────────────────────────────────────────────────


def machine_subs(ws_root):
    """(regex, replacement) pairs that make output independent of this machine."""
    home = os.path.expanduser('~')
    pairs = [(re.escape(os.path.realpath(ws_root)), SHOWN_ROOT), (re.escape(ws_root), SHOWN_ROOT)]
    pairs.append((re.escape(home) + r'(?=/|\b)', '~'))
    host = socket.gethostname()
    if host:
        pairs.append((re.escape(host), 'laptop'))
    user = getpass.getuser()
    if user and user not in ('root', 'student', 'runner'):
        pairs.append((rf'(?<![\w-]){re.escape(user)}(?![\w-])', 'student'))
    return pairs


DETERMINISTIC_SUBS = [
    (r'\d{8}-\d{6}', '20261115-160000'),  # log file names
    (r'(eurobot-(?:dev|cross):)[\w.-]+', r'\1develop'),  # DIT_DEV_TAG of the caller
]


def clean_env(cols, shim_dir, home, extra=None):
    """A minimal environment: no CI variables, no personal git config, fixed width."""
    env = {
        'PATH': shim_dir + ':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'HOME': home,
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'TERM': 'xterm-256color',
        'FORCE_COLOR': '1',
        'COLUMNS': str(cols),
        'LINES': '50',
        'PYTHONUNBUFFERED': '1',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHON_COLORS': '0',  # Python 3.13+ would color argparse help on its own
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.path.join(shim_dir, 'gitconfig'),
    }
    for k in ('DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CONTEXT', 'XDG_RUNTIME_DIR', 'DISPLAY'):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.update(extra or {})
    return env


def make_shim(tmp, ws_root=ROOT):
    """bin dir with `ditx` -> bin/ditx of ws_root, plus a neutral git identity."""
    d = os.path.join(tmp, 'shim')
    os.makedirs(d, exist_ok=True)
    # run ditx with this interpreter, not the first python3 on the clean PATH: a newer
    # system Python (3.13+ colors argparse help) would change every help shot
    link = os.path.join(d, 'ditx')
    if os.path.lexists(link):
        os.remove(link)
    with open(link, 'w', encoding='utf-8') as f:
        f.write(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} '
            f'{shlex.quote(os.path.join(ws_root, "bin", "ditx"))} "$@"\n'
        )
    os.chmod(link, 0o755)
    with open(os.path.join(d, 'gitconfig'), 'w', encoding='utf-8') as f:
        f.write(
            '[user]\n\tname = student\n\temail = student@example.com\n'
            '[init]\n\tdefaultBranch = develop\n[advice]\n\tdetachedHead = false\n'
        )
    return d


CACHES = {'__pycache__', '.ruff_cache', '.pytest_cache', '.mypy_cache'}


def copy_tree(dst):
    """This working tree without git, build output, synced domains or local settings."""
    top_skip = {'.git', 'build', 'logs', '.sim-robot', 'site', '.env', 'domains'}

    def ignore(path, names):
        skip = {n for n in names if n in CACHES}
        if os.path.realpath(path) == ROOT:
            skip |= top_skip & set(names)
        if os.path.realpath(path) == OUT_DIR:
            skip |= set(names)
        return skip

    shutil.copytree(ROOT, dst, ignore=ignore, symlinks=True)
    os.makedirs(os.path.join(dst, 'domains'), exist_ok=True)


def run_pipe(cmd, cwd, env, timeout):
    p = subprocess.run(
        ['bash', '-c', cmd],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return p.returncode, p.stdout.decode('utf-8', errors='replace')


def run_tty(cmd, cwd, env, timeout, cols, answer='', expect=''):
    """Run in a pseudo terminal `cols` wide; type `answer` once `expect` shows up."""
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 50, cols, 0, 0))
    p = subprocess.Popen(
        ['bash', '-c', cmd], cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave
    )
    os.close(slave)
    out, sent, deadline = b'', not answer, time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: the child closed the terminal
                break
            if not data:
                break
            out += data
            if not sent and expect.encode() in out:
                os.write(master, answer.encode())
                sent = True
        elif p.poll() is not None:
            break
    else:
        p.kill()
    os.close(master)
    return p.wait(), out.decode('utf-8', errors='replace')


def run_tmux(cmd, cwd, env, cols, rows, keys, timeout=60):
    """Drive a full-screen program in a private tmux server; return its final screen.

    keys: tmux key names (`Down`, `Tab`, `Enter`, `Space`, `n`), `text:<literal>` to type
    text, `wait:<seconds>` to pause. Each key is followed by a short settle delay.
    """
    sock = f'ditx-shots-{os.getpid()}'
    tmux = ['tmux', '-L', sock, '-f', '/dev/null']

    def run(*a):
        return subprocess.run(tmux + list(a), env=env, cwd=cwd, capture_output=True, text=True)

    try:
        run('new-session', '-d', '-s', 'shot', '-x', str(cols), '-y', str(rows), 'bash', '-c', cmd)
        run('set-option', '-t', 'shot', 'status', 'off')
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:  # wait for the first frame to settle
            time.sleep(0.3)
            screen = run('capture-pane', '-p', '-t', 'shot').stdout
            if screen.strip() and screen == last:
                break
            last = screen
        for key in keys:
            if key.startswith('wait:'):
                time.sleep(float(key[5:]))
            elif key.startswith('text:'):
                run('send-keys', '-t', 'shot', '-l', key[5:])
            else:
                run('send-keys', '-t', 'shot', key)
            time.sleep(0.3)
        time.sleep(0.5)
        lines = run('capture-pane', '-e', '-p', '-t', 'shot').stdout.split('\n')
        while lines and not re.sub(r'\x1b\[[0-9;]*m', '', lines[-1]).strip():
            lines.pop()  # a short screen leaves the rest of the pane blank
        return 0, '\n'.join(lines) + '\n'
    finally:
        run('kill-server')


def dev_image_present(env):
    tag = env.get('DIT_DEV_TAG') or os.environ.get('DIT_DEV_TAG') or 'develop'
    image = f'ghcr.io/seanchangx/eurobot-dev:{tag}'
    r = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True)
    return r.returncode == 0


def render(shot, raw, ws_root):
    subs = machine_subs(ws_root) + (DETERMINISTIC_SUBS if not shot.stateful else [])
    subs += list(shot.subs)
    text = raw
    for pattern, repl in subs:
        text = re.sub(pattern, repl, text)
    return termshot.render_svg(text, cols=shot.cols, title=shot.title, cmd=shot.show or shot.cmd)


def capture(shot, ws_root, shim, home, extra_env=None):
    env = clean_env(shot.cols, shim, home, extra_env)
    if shot.setup:
        rc, out = run_pipe(shot.setup, ws_root, env, shot.timeout)
        if rc:
            raise RuntimeError(f'setup of {shot.id} failed ({rc}):\n{out[-3000:]}')
    if shot.keys:
        if not shutil.which('tmux'):
            raise RuntimeError(f'{shot.id} needs tmux (sudo apt install tmux)')
        # an empty HOME: the TUI restores the last selection from ~/.config/ditx
        with tempfile.TemporaryDirectory(prefix='ditx-shot-home-') as fresh:
            env = {**env, 'HOME': fresh, 'COLUMNS': str(shot.cols), 'LINES': str(shot.rows)}
            return run_tmux(shot.cmd, ws_root, env, shot.cols, shot.rows, shot.keys)
    if shot.tty:
        return run_tty(shot.cmd, ws_root, env, shot.timeout, shot.cols, shot.answer, shot.expect)
    return run_pipe(shot.cmd, ws_root, env, shot.timeout)


def write_or_check(shot, svg, check):
    path = os.path.join(OUT_DIR, shot.id + '.svg')
    old = None
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            old = f.read()
    if check:
        return old == svg
    os.makedirs(OUT_DIR, exist_ok=True)
    if old != svg:
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(svg)
    return True


def svg_rows(svg):
    """The text of a rendered shot, one string per terminal row."""
    rows = {}
    for y, text in re.findall(r'<text x="[^"]*" y="([^"]*)"[^>]*>([^<]*)</text>', svg):
        rows.setdefault(float(y), []).append(html.unescape(text))
    return [''.join(parts) for _, parts in sorted(rows.items())]


def print_text_diff(shot, svg):
    path = os.path.join(OUT_DIR, shot.id + '.svg')
    old = svg_rows(open(path, encoding='utf-8').read()) if os.path.exists(path) else []
    new = svg_rows(svg)
    diff = list(difflib.unified_diff(old, new, 'committed', 'now', n=1, lineterm=''))
    for line in diff[:40] or ['  (same text: only colors or positions differ)']:
        print(f'    {line}', file=sys.stderr)


def run_deterministic(shots, check, tmp):
    shim = make_shim(tmp)
    home = os.path.join(tmp, 'home')
    os.makedirs(home, exist_ok=True)
    extra = {'DIT_DEV_TAG': os.environ['DIT_DEV_TAG']} if os.environ.get('DIT_DEV_TAG') else {}
    stale, skipped = [], []
    for shot in shots:
        if 'dev-image' in shot.needs and not dev_image_present(extra):
            skipped.append((shot.id, 'dev image not available'))
            continue
        ws = os.path.join(tmp, 'ws-' + shot.id)
        copy_tree(ws)
        rc, raw = capture(shot, ws, shim, home, extra)
        svg = render(shot, raw, ws)
        ok = write_or_check(shot, svg, check)
        shutil.rmtree(ws, ignore_errors=True)
        print(f'{"ok   " if ok else "STALE"} {shot.id}  (exit {rc})', file=sys.stderr)
        if not ok:
            stale.append(shot.id)
            if check:  # say what differs: a red CI job must be explainable from its log
                print_text_diff(shot, svg)
    return stale, skipped


def sandbox_ws(sandbox):
    return os.path.join(os.path.abspath(sandbox), 'DIT-X')


def rerender(shots, sandbox):
    """Render stateful shots again from their saved raw output (after a theme change)."""
    ws = sandbox_ws(sandbox)
    for shot in shots:
        path = os.path.join(os.path.abspath(sandbox), 'raw', shot.id + '.ansi')
        if not os.path.exists(path):
            print(f'no raw output for {shot.id}', file=sys.stderr)
            continue
        with open(path, encoding='utf-8') as f:
            write_or_check(shot, render(shot, f.read(), ws), False)
        print(f'ok    {shot.id}  (re-rendered)', file=sys.stderr)


def run_stateful(shots, check, sandbox, tmp):
    ws = sandbox_ws(sandbox)
    if not os.path.isfile(os.path.join(ws, 'ditx.repos')):
        sys.exit(f'no sandbox workspace at {ws}: run with --prepare first')
    shim = make_shim(tmp, ws)  # the sandbox's own ditx, as if installed there
    extra = {'COMPOSE_PROJECT_NAME': SHOT_PROJECT}
    stale = []
    for shot in shots:
        t0 = time.monotonic()
        rc, raw = capture(shot, ws, shim, os.path.expanduser('~'), extra)
        raw_dir = os.path.join(os.path.abspath(sandbox), 'raw')  # kept for debugging
        os.makedirs(raw_dir, exist_ok=True)
        with open(os.path.join(raw_dir, shot.id + '.ansi'), 'w', encoding='utf-8') as f:
            f.write(raw)
        ok = write_or_check(shot, render(shot, raw, ws), check)
        took = time.monotonic() - t0
        print(f'{"ok   " if ok else "STALE"} {shot.id}  (exit {rc}, {took:.0f}s)', file=sys.stderr)
        if not ok:
            stale.append(shot.id)
    return stale


# ── sandbox ──────────────────────────────────────────────────────────────────


def sh(cmd, cwd=None, env=None):
    print(f'$ {cmd}', file=sys.stderr)
    subprocess.run(['bash', '-c', cmd], cwd=cwd, env=env, check=True)


def prepare(sandbox, repos_base, branch):
    """Fresh clone of this repo + domains pointed at `repos_base` + sandbox sim robot."""
    sandbox = os.path.abspath(sandbox)
    ws = sandbox_ws(sandbox)
    if os.path.exists(ws):
        sys.exit(f'{ws} exists: --teardown first')
    os.makedirs(sandbox, exist_ok=True)
    sh(f'git clone -q -b {shlex.quote(branch)} {shlex.quote(ROOT)} {shlex.quote(ws)}')
    if repos_base:
        names = {'main': 'Main', 'navigation': 'Navigation', 'localization': 'Localization'}
        names['vision'] = 'Vision'
        path = os.path.join(ws, 'ditx.repos')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        for d, n in names.items():
            text = re.sub(
                rf'(domains/{d}:\n\s+type: git\n\s+url: )\S+',
                rf'\g<1>{repos_base.rstrip("/")}/Eurobot-{n}',
                text,
            )
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    ini = os.path.join(ws, 'config', 'robots.ini')
    with open(ini, encoding='utf-8') as f:
        text = f.read()
    text = re.sub(r'(\[sim\][^\[]*?domain_id = )\d+', rf'\g<1>{SHOT_DOMAIN_ID}', text)
    with open(ini, 'w', encoding='utf-8') as f:
        f.write(text)
    with open(os.path.join(ws, '.env'), 'w', encoding='utf-8') as f:
        f.write('DIT_DEV_TAG=local\nDIT_ROBOT=sim\n')
    # local edits that `ditx status` / `ditx sync` should not report as a dirty shell repo
    sh('git update-index --skip-worktree ditx.repos config/robots.ini', cwd=ws)
    print(f'sandbox ready: {ws}', file=sys.stderr)


def teardown(sandbox, cleaner_image):
    sandbox = os.path.abspath(sandbox)
    ws = sandbox_ws(sandbox)
    if os.path.isdir(os.path.join(ws, '.sim-robot')):
        env = {**os.environ, 'COMPOSE_PROJECT_NAME': SHOT_PROJECT}
        home = os.path.join(ws, '.sim-robot')
        sh(
            f'docker compose -p {SHOT_PROJECT} --project-directory {home} -f {home}/compose.yaml '
            f'--env-file {home}/.env down --remove-orphans',
            env=env,
        )
    if os.path.isdir(sandbox):
        # build output and robot state can be owned by root (containers): remove in one
        sh(
            f'docker run --rm -v {shlex.quote(sandbox)}:/t {cleaner_image} '
            'sh -c "rm -rf /t/* /t/.[!.]*"'
        )
        os.rmdir(sandbox)
    print('sandbox removed', file=sys.stderr)


# ── docs mapping ─────────────────────────────────────────────────────────────


def text_blocks(docs_dir):
    """[(doc path relative to docs/, line number, first line)] of every ```text block."""
    out = []
    for path in sorted(glob.glob(os.path.join(docs_dir, '**', '*.md'), recursive=True)):
        with open(path, encoding='utf-8') as f:
            lines = f.read().split('\n')
        for i, line in enumerate(lines):
            if re.match(r'^\s*```text\b', line) and i + 1 < len(lines):
                out.append((os.path.relpath(path, docs_dir), i + 1, lines[i + 1].strip()))
    return out


def write_map(path):
    docs_dir = os.path.join(ROOT, 'docs')
    blocks = text_blocks(docs_dir)
    rows, missing, used = [], [], set()
    for doc, line, first in blocks:
        ids = [s.id for s in SHOTS for d, f in s.docs if d == doc and first.startswith(f.strip())]
        why = [w for d, f, w in NOT_SHOTS if d == doc and first.startswith(f.strip())]
        rows.append((doc, line, first, ids, why))
        used.update(ids)
        if not ids and not why:
            missing.append(f'{doc}:{line}')
    md = [
        '# Terminal shots → docs',
        '',
        'Generated by `python3 tools/docs/shots.py --map`. Each row is one ```` ```text ````',
        'block in `docs/`; replace it with the image(s), e.g.',
        '`![ditx sync](../assets/term/sync.svg)` (path relative to the page).',
        'Kind: **det** = deterministic, regenerated and drift-checked in CI',
        '(`shots.py --check`); **state** = needs the prepared sandbox (`shots.py --stateful`).',
        '',
        '| Doc | Line | Block starts with | Shot(s) |',
        '|---|---|---|---|',
    ]
    kind = {s.id: 'state' if s.stateful else 'det' for s in SHOTS}
    for doc, line, first, ids, why in rows:
        cell = first.replace('|', '\\|')
        if len(cell) > 70:
            cell = cell[:69] + '…'
        target = (
            '<br>'.join(f'`{i}.svg` ({kind[i]})' for i in ids)
            if ids
            else f'— ({why[0]})'
            if why
            else '— **unmapped**'
        )
        md.append(f'| `{doc}` | {line} | `{cell}` | {target} |')
    extra = [s.id for s in SHOTS if s.id not in used]
    if extra:
        md += ['', 'Shots not tied to a block yet: ' + ', '.join(f'`{i}`' for i in extra) + '.']
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(md) + '\n')
    return missing


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='shots.py',
        description='Regenerate (or --check) the terminal screenshots in docs/assets/term/.',
    )
    p.add_argument('ids', nargs='*', help='only these shots (stateful ones need --sandbox)')
    p.add_argument('--check', action='store_true', help='exit 1 if an image is stale')
    p.add_argument('--stateful', action='store_true', help='also run the stateful shots')
    p.add_argument('--sandbox', help='directory of the stateful sandbox (e.g. ../_shots)')
    p.add_argument('--prepare', action='store_true', help='create the sandbox and exit')
    p.add_argument('--teardown', action='store_true', help='stop and remove the sandbox')
    p.add_argument(
        '--fresh',
        action='store_true',
        help='one command for the stateful shots: --prepare, run them all, --teardown',
    )
    p.add_argument('--repos-base', help='--prepare: base URL of the domain repos (file:///...)')
    p.add_argument('--branch', default='develop', help='--prepare: branch to clone')
    p.add_argument(
        '--cleaner-image',
        default='ghcr.io/seanchangx/eurobot-dev:local',
        help='--teardown: image used to delete root-owned files',
    )
    p.add_argument(
        '--rerender',
        action='store_true',
        help='stateful shots: render the saved raw output again instead of running',
    )
    p.add_argument('--list', action='store_true', help='list the shots')
    p.add_argument('--map', action='store_true', help='rewrite docs/assets/term/MAP.md')
    a = p.parse_args(argv)

    if a.list:
        for s in SHOTS:
            kind = 'stateful' if s.stateful else 'deterministic'
            print(f'{s.id:28} {kind:13} {s.cmd}')
        return 0
    if a.map:
        missing = write_map(os.path.join(OUT_DIR, 'MAP.md'))
        for m in missing:
            print(f'unmapped ```text block: docs/{m}', file=sys.stderr)
        return 0
    if a.prepare or a.teardown:
        if not a.sandbox:
            p.error('--prepare / --teardown need --sandbox DIR')
        if a.prepare:
            prepare(a.sandbox, a.repos_base, a.branch)
        else:
            teardown(a.sandbox, a.cleaner_image)
        return 0

    if a.fresh:
        if not a.sandbox:
            p.error('--fresh needs --sandbox DIR')
        prepare(a.sandbox, a.repos_base, a.branch)
        try:
            with tempfile.TemporaryDirectory(prefix='ditx-shots-') as tmp:
                run_stateful([s for s in SHOTS if s.stateful], False, a.sandbox, tmp)
        finally:
            teardown(a.sandbox, a.cleaner_image)
        return 0

    known = {s.id for s in SHOTS}
    unknown = [i for i in a.ids if i not in known]
    if unknown:
        p.error('unknown shot(s): ' + ', '.join(unknown))
    picked = [s for s in SHOTS if (s.id in a.ids if a.ids else (a.stateful or not s.stateful))]
    det = [s for s in picked if not s.stateful]
    state = [s for s in picked if s.stateful]
    if state and not a.sandbox:
        p.error('stateful shots need --sandbox DIR (prepared with --prepare)')

    if a.rerender:
        rerender(state, a.sandbox)
        det = []
        state = []
    with tempfile.TemporaryDirectory(prefix='ditx-shots-') as tmp:
        stale, skipped = run_deterministic(det, a.check, tmp)
        if state:
            stale += run_stateful(state, a.check, a.sandbox, tmp)
    for sid, why in skipped:
        print(f'skipped {sid}: {why}', file=sys.stderr)
    if a.check and stale:
        print(
            f'{len(stale)} stale image(s): {", ".join(stale)}\n'
            'regenerate: python3 tools/docs/shots.py ' + ' '.join(stale),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
