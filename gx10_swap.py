#!/usr/bin/env python3
"""gx10-swap (gswap) - swap whole project stacks on the shared-GPU GX10 box.

Two GPU projects (NidaMind, CestusAI, ...) can't run on one GB10 at once, so this
flips between them over SSH: stop one stack, start another. Projects are declared
in projects.toml - add a block to onboard a new one; no code changes.

Stdlib only (Python 3.11+ for tomllib). Usage:

  gswap status                 # what's up on the GX10 + GPU holders
  gswap list                   # configured projects
  gswap up <project>           # activate + start a project's stack
  gswap down <project>         # stop a project's stack
  gswap switch [project]       # stop other GPU projects, then bring this one up
                               #   no arg with exactly 2 GPU projects -> flip to the other
                               #   no arg otherwise -> interactive picker
  gswap restart <project>      # stop then start a project's stack
  gswap stop-all               # stop every configured project

Projects can be named by key, unambiguous prefix ("nida"), or list index ("1").
Global flags: --dry-run (print commands, run nothing), --json (machine output for
list/status), -y/--yes (skip confirmation prompts).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

CONFIG = Path(__file__).with_name("projects.toml")

# UTF-8 stdout so symbols never blow up the legacy Windows (cp1252) console.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# Color only where the terminal actually supports ANSI (Windows Terminal / *nix
# tty). The legacy conhost shows raw escape codes, so stay plain there.
_USE_COLOR = sys.stdout.isatty() and (
    os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
)

# Set by --dry-run: print remote commands instead of running them.
DRY_RUN = False


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


GREEN = lambda s: _c("32", s)   # noqa: E731
RED = lambda s: _c("31", s)     # noqa: E731
DIM = lambda s: _c("2", s)      # noqa: E731
BOLD = lambda s: _c("1", s)     # noqa: E731
CYAN = lambda s: _c("36", s)    # noqa: E731
YELLOW = lambda s: _c("33", s)  # noqa: E731


def load() -> dict:
    if not CONFIG.exists():
        example = CONFIG.with_name("projects.example.toml")
        hint = f"\nCopy the template to get started:\n  cp {example.name} {CONFIG.name}" if example.exists() else ""
        sys.exit(f"config not found: {CONFIG}{hint}")
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    if "ssh" not in cfg or "projects" not in cfg:
        sys.exit("projects.toml must define `ssh` and a [projects.*] table")
    if not cfg["projects"]:
        sys.exit("projects.toml defines no [projects.*] blocks")
    return cfg


_SSH_OPTS = [
    "-o", "BatchMode=yes",            # never prompt - fail fast if auth/key isn't set up
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]


def _resolve_ssh() -> str:
    """Find a usable ssh.

    Order: explicit GSWAP_SSH override -> `ssh` on PATH -> Git's bundled ssh.exe.
    The Git fallback matters on Windows/PowerShell: the system OpenSSH often lacks
    the key/agent this box is set up with, while Git Bash's ssh has it. Auto-finding
    it means `gswap` works from PowerShell with no GSWAP_SSH or Git Bash needed.
    """
    override = os.environ.get("GSWAP_SSH")
    if override:
        return override

    from shutil import which

    found = which("ssh")
    if found and os.name != "nt":
        return found  # on *nix a PATH ssh is fine

    # On Windows, prefer Git's ssh (has the agent/keys); fall back to PATH ssh.
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Git\usr\bin\ssh.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\usr\bin\ssh.exe"),
        os.path.expandvars(r"%LocalAppData%\Programs\Git\usr\bin\ssh.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return found or "ssh"


_SSH_BIN = _resolve_ssh()  # override with GSWAP_SSH if auto-detection picks the wrong one


def ssh_run(host: str, remote_cmd: str, capture: bool = False) -> tuple[int, str]:
    """Run one command on the GX10. Returns (exit_code, output)."""
    if DRY_RUN:
        # Capturing callers (status checks) get a benign empty result; step
        # runners print the command via run_steps and never reach here.
        return 0, ""
    proc = subprocess.run(
        [_SSH_BIN, *_SSH_OPTS, host, remote_cmd],
        capture_output=capture,
        text=True,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip() if capture else ""
    return proc.returncode, out


def run_steps(host: str, steps: list[str], heading: str) -> bool:
    """Run a project's command list sequentially, streaming progress."""
    print(BOLD(heading))
    ok = True
    for step in steps:
        short = step if len(step) <= 90 else step[:87] + "..."
        if DRY_RUN:
            print(YELLOW(f"  (dry-run) $ {short}"))
            continue
        print(DIM(f"  $ {short}"))
        code, _ = ssh_run(host, step)
        if code != 0:
            print(RED(f"  ! step exited {code} (continuing)"))
            ok = False
    return ok


def is_up(host: str, project: dict) -> bool:
    check = project.get("status")
    if not check:
        return False
    _, out = ssh_run(host, check, capture=True)
    out = out.strip().lower()
    return out in ("active", "true") or out.endswith("true") or out == "active"


def wait_until_up(host: str, project: dict, attempts: int = 5, delay: float = 2.0) -> bool:
    """Poll the project's status check a few times so a slow-starting stack
    still reports ✓ instead of a misleading 'verify manually'."""
    if not project.get("status") or DRY_RUN:
        return is_up(host, project)
    for i in range(attempts):
        if is_up(host, project):
            return True
        if i < attempts - 1:
            time.sleep(delay)
    return False


def _resolve(cfg: dict, name: str) -> str:
    """Map a user token (key, unambiguous prefix, or 1-based index) to a project key."""
    keys = list(cfg["projects"])
    if name in keys:
        return name
    if name.isdigit():
        idx = int(name) - 1
        if 0 <= idx < len(keys):
            return keys[idx]
        sys.exit(f"index {name} out of range (1-{len(keys)})")
    matches = [k for k in keys if k.startswith(name.lower())]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sys.exit(f"ambiguous '{name}' - matches {', '.join(matches)}")
    sys.exit(f"unknown project '{name}'. Configured: {', '.join(keys)}")


def _project(cfg: dict, name: str) -> dict:
    return cfg["projects"][_resolve(cfg, name)]


def _confirm(args, prompt: str) -> bool:
    if getattr(args, "yes", False) or DRY_RUN or not sys.stdin.isatty():
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# - commands -
def _project_states(cfg: dict) -> list[tuple[str, dict, bool]]:
    host = cfg["ssh"]
    return [(key, p, is_up(host, p)) for key, p in cfg["projects"].items()]


def cmd_list(cfg: dict, args) -> None:
    states = _project_states(cfg)
    if getattr(args, "json", False):
        print(json.dumps({
            "host": cfg["ssh"],
            "projects": [
                {"key": k, "label": p.get("label", k), "gpu": bool(p.get("gpu")), "up": up}
                for k, p, up in states
            ],
        }, indent=2))
        return
    print(BOLD(f"Projects on {cfg['ssh']}:"))
    for i, (key, p, up) in enumerate(states, 1):
        dot = GREEN("● up") if up else DIM("○ down")
        gpu = CYAN(" [gpu]") if p.get("gpu") else ""
        print(f"  {DIM(f'{i}.')} {dot}  {key:<12} {p.get('label', key)}{gpu}")


def cmd_status(cfg: dict, args) -> None:
    host = cfg["ssh"]
    cmd_list(cfg, args)
    if getattr(args, "json", False):
        return
    print(BOLD("\nGPU holders right now:"))
    lines: list[str] = []

    # Per-project hint: each gpu project may declare `gpu_check`, a remote command
    # that prints something when that project holds the GPU (e.g. the service/proc
    # name). Honest for any project you add - no hardcoded service names.
    for key, p in cfg["projects"].items():
        if not (p.get("gpu") and p.get("gpu_check")):
            continue
        _, out = ssh_run(host, p["gpu_check"], capture=True)
        if out.strip():
            label = p.get("label", key)
            for ln in out.strip().splitlines():
                lines.append(f"  {label}: {ln.strip()}")

    # Always-on fallback sweep: any running container, regardless of project.
    _, containers = ssh_run(
        host,
        "for c in $(docker ps --format '{{.Names}}'); do echo \"  container $c\"; done",
        capture=True,
    )
    if containers.strip():
        lines.extend(containers.rstrip().splitlines())

    print("\n".join(lines) if lines else DIM("  (none reported)"))


def cmd_up(cfg: dict, args) -> None:
    host = cfg["ssh"]
    key = _resolve(cfg, args.project)
    p = cfg["projects"][key]
    if p.get("activate"):
        run_steps(host, p["activate"], f"Preparing {p.get('label', key)}...")
    run_steps(host, p["start"], f"Starting {p.get('label', key)}...")
    if DRY_RUN:
        return
    if wait_until_up(host, p):
        print(GREEN(f"✓ {key} up"))
    else:
        print(YELLOW("started (status not confirmed yet - re-check with `gswap status`)"))


def cmd_down(cfg: dict, args) -> None:
    host = cfg["ssh"]
    key = _resolve(cfg, args.project)
    p = cfg["projects"][key]
    run_steps(host, p["stop"], f"Stopping {p.get('label', key)}...")
    if not DRY_RUN:
        print(GREEN(f"✓ {key} stopped"))


def cmd_restart(cfg: dict, args) -> None:
    cmd_down(cfg, args)
    cmd_up(cfg, args)


def _pick_switch_target(cfg: dict, args) -> str:
    """Resolve the switch target: explicit arg, the-other-one toggle, or picker."""
    if args.project:
        return _resolve(cfg, args.project)

    gpu_keys = [k for k, p in cfg["projects"].items() if p.get("gpu")]
    host = cfg["ssh"]

    # Exactly two GPU projects -> flip to whichever isn't currently up.
    if len(gpu_keys) == 2:
        up = [k for k in gpu_keys if is_up(host, cfg["projects"][k])]
        if len(up) == 1:
            return gpu_keys[1] if up[0] == gpu_keys[0] else gpu_keys[0]
        # none or both up - fall through to picker

    # Interactive picker.
    if not sys.stdin.isatty():
        sys.exit("`switch` needs a project (no TTY for interactive pick)")
    cmd_list(cfg, args)
    keys = list(cfg["projects"])
    try:
        choice = input(BOLD("\nSwitch to which? (number or name) ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not choice:
        sys.exit("no selection")
    return _resolve(cfg, choice)


def cmd_switch(cfg: dict, args) -> None:
    host = cfg["ssh"]
    target_key = _pick_switch_target(cfg, args)
    args.project = target_key  # so cmd_up resolves the same project
    # Stop every OTHER gpu project first so the GPU is free for the target.
    for key, p in cfg["projects"].items():
        if key != target_key and p.get("gpu"):
            run_steps(host, p["stop"], f"Stopping {p.get('label', key)} (freeing GPU)...")
    cmd_up(cfg, args)
    if not DRY_RUN:
        print(GREEN(f"\n✓ switched to {target_key}"))


def cmd_stop_all(cfg: dict, args) -> None:
    host = cfg["ssh"]
    if not _confirm(args, "Stop EVERY configured project on the GX10?"):
        sys.exit("aborted")
    for key, p in cfg["projects"].items():
        run_steps(host, p["stop"], f"Stopping {p.get('label', key)}...")
    if not DRY_RUN:
        print(GREEN("✓ all projects stopped"))


def main() -> None:
    cfg = load()
    parser = argparse.ArgumentParser(prog="gswap", description="Swap GPU project stacks on the GX10.")
    parser.add_argument("--dry-run", action="store_true", help="print remote commands without running them")
    parser.add_argument("--json", action="store_true", help="machine-readable output (list/status)")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list configured projects + up/down").set_defaults(fn=cmd_list)
    sub.add_parser("status", help="show up/down + GPU holders").set_defaults(fn=cmd_status)
    for name, fn, help_ in (("up", cmd_up, "activate + start a project"),
                            ("down", cmd_down, "stop a project"),
                            ("restart", cmd_restart, "stop then start a project")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("project")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("switch", help="stop other GPU projects then bring this one up (no arg: flip/pick)")
    sp.add_argument("project", nargs="?", help="key, prefix, or index; omit to flip or pick")
    sp.set_defaults(fn=cmd_switch)
    sub.add_parser("stop-all", help="stop every project").set_defaults(fn=cmd_stop_all)

    # claude-swap users reach for `gswap --status` / `--list`; accept the dashed
    # form for any subcommand by normalizing it to the bare name before parsing.
    commands = set(sub.choices)
    argv = [
        (a[2:] if a.startswith("--") and a[2:] in commands else a)
        for a in sys.argv[1:]
    ]
    args = parser.parse_args(argv)

    global DRY_RUN
    DRY_RUN = args.dry_run

    args.fn(cfg, args)


if __name__ == "__main__":
    main()
