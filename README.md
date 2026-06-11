# gx10-swap (`gswap`)

A tiny, dependency-free CLI to **swap whole project stacks on the shared-GPU GX10 box**.
The GB10 has one GPU; two GPU projects (NidaMind, CestusAI, …) can't run at once. `gswap`
flips between them over SSH — stop one stack, start another — and is extensible: add a
project by editing `projects.toml`, no code changes.

Inspired by the ergonomics of [claude-swap](https://github.com/realiti4/claude-swap),
but for project stacks instead of accounts.

## Requirements
- Python 3.11+ (uses stdlib `tomllib`) on your Windows machine.
- A working **non-interactive** `ssh user@your-gx10-host` from the shell you run `gswap` in.
  - On this machine that's **Git Bash** (its ssh has the key/agent). Windows OpenSSH in
    PowerShell only works if the key is in `C:\Users\<you>\.ssh\` and trusted — if PowerShell's
    `ssh` prompts/hangs, run `gswap` from Git Bash, or point it at a specific ssh:
    `set GSWAP_SSH=C:\Program Files\Git\usr\bin\ssh.exe` (env var override).
- The SSH user has `sudo` (used for `systemctl`).

## Install
Add this folder to your PATH (so `gswap` works anywhere), e.g. in PowerShell:
```powershell
$env:Path += ";E:\gx10-swap"   # or add permanently via System > Environment Variables
```
Then use `gswap <command>` (the `gswap.cmd` shim calls `python gx10_swap.py`).

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
gswap switch nidamind     # work on NidaMind (CestusAI stopped, vLLM gets 80% of the GPU)
gswap switch              # later, just toggle to the other GPU project
gswap stop-all            # done for the day
```

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
```
`switch myproj` will then stop all other `gpu = true` projects before starting it.

## How it works
Each command list runs sequentially over `ssh <host> "<cmd>"`. `switch` enforces GPU
exclusivity. NidaMind's `activate` raises vLLM's `--gpu-memory-utilization` to 0.80 (safe
once CestusAI is stopped — with both up there's only ~79 GB free, which is why 0.85 OOM'd).
