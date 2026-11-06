# lean-throttle

Large Lean builds can overwhelm a machine's RAM or CPUs, especially when several
people, agents, or CI jobs build at once. **lean-throttle inserts sleep waits at
each Lean compiler invocation when memory or CPU usage is high**, letting existing
work finish before starting more. This helps avoid capacity limits and keeps a
shared build machine responsive.

Deploy it once in the environment that launches your builds. Users and agents
continue to run **`lake build`** as usual: no new commands, flags, project settings,
or knowledge of the throttle required.

One executable Python file. Linux, Python 3.10+, and an existing Lean 4/Lake
installation; no Python packages, daemon, or root privileges needed.

## Deploy

Clone somewhere persistent:

```sh
git clone https://github.com/kim-em/lean-throttle.git "$HOME/lean-throttle"
```

Add these lines to the environment used to launch your shells, agents, or CI jobs
(for an interactive Bash shell, `~/.bashrc`):

```sh
export LAKE_OVERRIDE_LEAN=true
export LEAN="$HOME/lean-throttle/lean-throttle"
unset LEAN_SYSROOT
```

Start a new shell or restart the relevant agent/service so it inherits those
settings. Every `lake build` launched in that environment now uses the throttle.
Keep both exports: Lake normally prefers its bundled compiler, and
`LAKE_OVERRIDE_LEAN=true` enables the override. An existing `LEAN_SYSROOT` takes
precedence over `LEAN`, so clear it when setting up the environment.

For CI, set these variables for the build step. For a systemd service, use
absolute paths:

```ini
[Service]
Environment=LAKE_OVERRIDE_LEAN=true
Environment=LEAN=/opt/lean-throttle/lean-throttle
UnsetEnvironment=LEAN_SYSROOT
```

The service's user needs Python on `PATH` and a writable home/cache directory.
Setting this up in an interactive shell does not change already-running services
or agents; put the settings in their launch environment too.

## Tune headroom

If your deployment needs different headroom, edit the two constants at the top of
`lean-throttle`:

| Constant | Default | Start Lean when… |
| --- | --- | --- |
| `CPU_LIMIT_PERCENT` | `90` | host CPU busy time is at most 90% |
| `MEMORY_LIMIT_PERCENT` | `80` | effective memory usage is at most 80% |

For unusually memory-hungry files, lower the memory threshold to leave more RAM
free. Changes apply to newly launched builds; no user or agent instructions need
to change.

## How it works

Before each compilation, the wrapper samples CPU activity over a random
0.2–0.5-second interval and reads available memory. If either threshold is
exceeded, it sleeps for a random 1–5 seconds and checks again, until both pass.
Random waits spread out simultaneous starts across independent builds.

Memory comes from Linux's `MemAvailable`, further bounded by visible cgroup v2
`memory.max` limits, including ancestors. Clean file cache counts as reclaimable;
shared memory and dirty/writeback pages do not. `memory.high` is a soft reclaim
threshold and does not redefine capacity. CPU usage comes from host `/proc/stat`
counters, with idle and I/O wait excluded from busy time.

Lake discovers its compiler through `lean --print-prefix` and then invokes
`<prefix>/bin/lean`. To keep those invocations throttled, the wrapper creates a
cached toolchain overlay: a copy of itself at `bin/lean`, symlinks to the original
libraries and tools, and a saved path to the original compiler. Installed
toolchains stay untouched. See [Lake's installation discovery source][lake-source].

Discovery probes (`--print-prefix`, `--githash`, `--version`, `--help`,
`--print-libdir`, and their short forms) skip the wait. Compilation stays silent,
including while waiting, so `lake build --iofail` works. Once admitted, the wrapper
replaces itself with the real compiler using `exec`, preserving arguments,
exit status, and normal signal handling.

This is cooperative admission control: it delays new compiler processes, and
does not suspend or limit already-running ones. Several invocations can pass
together, a single file can exceed available RAM, and sustained external load can
keep a build waiting indefinitely. It reduces contention without guaranteeing an
OOM-free build or enforcing a hard CPU/memory cap. Cgroup v1 memory limits,
container-hidden ancestor limits, and per-container CPU quotas are not measured;
CPU checks use the host's total activity. C compilation and linking are outside
the throttle. If required Linux counters are unreadable, the wrapper reports an
error instead of starting a compiler without checking capacity.

## Update or remove

Update with `git -C "$HOME/lean-throttle" pull`. New builds pick up the new script;
already-running builds keep their cached copy. Overlays live in
`${XDG_CACHE_HOME:-$HOME/.cache}/lean-throttle` and can be deleted when no builds
are using them.

To disable, remove the two exports from the launch environment and start new
shells/services. In a shell where you enabled it:

```sh
unset LEAN LAKE_OVERRIDE_LEAN LEAN_SYSROOT LEAN_THROTTLE_REAL_LEAN
```

## Development

```sh
python3 -m unittest discover -s tests -v
```

To also test compiler routing with an installed toolchain (no downloads):

```sh
LEAN_THROTTLE_TEST_TOOLCHAIN=/path/to/toolchain python3 -m unittest discover -s tests -v
```

Extracted and simplified from [TauCetiWorker's compiler wrapper][original].

[lake-source]: https://github.com/leanprover/lean4/blob/master/src/lake/Lake/Config/InstallPath.lean
[original]: https://github.com/TauCetiProject/TauCetiWorker/blob/99e7b174564ff378d563e81c9bb0265b06075ace/scripts/tauceti-lean
