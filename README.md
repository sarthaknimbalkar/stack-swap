# gx10-swap (`gswap`)

A tiny, dependency-free CLI to **swap whole project stacks on a shared-GPU box**
(built for an NVIDIA GB10 / Grace-Blackwell, but nothing is specific to it). When one
GPU can't host two GPU projects at once, `gswap` flips between them over SSH — stop one
stack, start another — and is extensible: add a project by editing `projects.toml`, no
code changes.

Inspired by the ergonomics of [claude-swap](https://github.com/realiti4/claude-swap),
but for project stacks instead of accounts.

## Requirements
- Python 3.11+ (uses stdlib `tomllib`) on your Windows machine.
- A working **non-interactive** (key-based) SSH login to your box.
- The SSH user has `sudo` (used for `systemctl`).

## Configure
Copy the template and edit it with your own host, paths, and stacks:
```powershell
Copy-Item projects.example.toml projects.toml   # bash: cp projects.example.toml projects.toml
```
`projects.toml` is **gitignored** — your real host and paths stay on your machine and
never get committed. See [`projects.example.toml`](projects.example.toml) for the format.

### Works from PowerShell, cmd, or Git Bash
`gswap` auto-detects a usable `ssh`: it uses `$env:GSWAP_SSH` if set, otherwise `ssh`
on PATH, and on Windows it **prefers Git's bundled `ssh.exe`** (which typically has the
key/agent configured). That means it just works from PowerShell — no need to launch
Git Bash or set anything by hand. If auto-detection picks the wrong ssh, override it:
```powershell
$env:GSWAP_SSH = "C:\Program Files\Git\usr\bin\ssh.exe"
```
The installer (below) also registers a native PowerShell `gswap` function, so PowerShell
runs the script directly without going through the `cmd.exe` shim.

## Install
Run the installer once (PowerShell). It drops a `gswap` shim into
`%USERPROFILE%\.local\bin` and ensures that folder is on your persistent user PATH:
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```
Open a **new** terminal and `gswap <command>` works from anywhere. The shim calls
this repo's `gx10_swap.py` directly, so edits to the script take effect with no
reinstall. Uninstall with `.\install.ps1 -Uninstall`.

> Manual alternative (no installer): add this folder to PATH yourself —
> `$env:Path += ";E:\gx10-swap"` — and use the bundled `gswap.cmd` shim.

## Commands
```
gswap status              # what's up on the GX10 + which containers/services hold the GPU
gswap list                # configured projects and whether each is up
gswap up <project>        # activate (e.g. set vLLM GPU share) + start a project's stack
gswap down <project>      # stop a project's stack
gswap restart <project>   # stop then start a project's stack
gswap switch [project]    # stop every OTHER gpu project, then bring this one up   <-- the main one
gswap stop-all            # stop every configured project (free the box)
```

### Naming projects
Anywhere a `<project>` is expected you can use the **key** (`nidamind`), an
**unambiguous prefix** (`nida`), or its **list index** (`1`, as shown by `gswap list`).

### `switch` with no argument
- **Exactly two GPU projects and one is up** → flips to the other (the everyday case):
  just type `gswap switch` to toggle.
- **Otherwise** (none/both up, or 3+ GPU projects) → shows the list and prompts you to pick.

### Global flags
```
--dry-run    print the remote commands that would run, execute nothing (great for a first look)
--json       machine-readable output for `list` / `status`
-y, --yes    skip confirmation prompts (e.g. for stop-all in scripts)
```

After `up`/`switch`, gswap polls the project's `status` check for a few seconds so a
slow-starting stack still reports `✓ up` instead of a misleading "verify manually".

Typical day:
```
gswap switch project-a    # work on Project A (Project B stopped, A gets the whole GPU)
gswap switch              # later, just toggle to the other GPU project
gswap stop-all            # done for the day
```

## Shared / standalone services
Alongside the GPU project stacks, you can declare **shared services** with
`gpu = false` so `switch` never auto-stops them — handy for a service an active
project depends on (e.g. an embeddings backend) that must survive a switch.

Because they're `gpu = false`, they don't participate in GPU exclusivity — but they
still show up in `gswap status` / `gswap list` and are included in `gswap stop-all`
(so "stop everything for the day" really stops everything). Toggle them explicitly
with `gswap up|down <name>`.

## Adding a project
Copy a block in `projects.toml`:
```toml
[projects.myproj]
label = "My Project"
gpu = true                       # set true if it monopolizes the GPU
activate = [ "..." ]             # optional: runs before start (e.g. tune GPU share)
start    = [ "docker compose -f /path/compose.yml up -d", "sudo systemctl start mysvc" ]
stop     = [ "sudo systemctl stop mysvc", "docker compose -f /path/compose.yml stop" ]
status   = "systemctl is-active mysvc 2>/dev/null || true"   # prints 'active'/'true' when up
gpu_check = "systemctl is-active mysvc >/dev/null 2>&1 && echo 'mysvc (active)'"  # optional
```
`gpu_check` is optional and only consulted for `gpu = true` projects: `gswap status`
runs it and lists whatever it prints under "GPU holders", so a new GPU project is
reported honestly instead of being invisible. `status` always also sweeps running
containers via `docker ps`, so you get that for free even without `gpu_check`.
`switch myproj` will then stop all other `gpu = true` projects before starting it.

## How it works
Each command list runs sequentially over `ssh <host> "<cmd>"`. `switch` enforces GPU
exclusivity by stopping every other `gpu = true` project before starting the target.
A project's optional `activate` step runs before `start` — useful for handing the whole
GPU to the active project once the other is down (e.g. raising an inference server's
memory-utilization knob now that nothing else is competing for the pool).

## Security / privacy
- **No secrets in the repo.** Auth is your SSH key (`BatchMode=yes`, so it fails fast
  instead of prompting); no passwords or tokens are stored or read by `gswap`.
- **Your host and paths stay local** — they live only in `projects.toml`, which is
  gitignored. Only the sanitized `projects.example.toml` is committed.
- Remote command lists in `projects.toml` are run verbatim on your box, so treat that
  file as you would any shell script you run with `sudo`.
