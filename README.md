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

Each invocation checks memory and recent CPU usage immediately. CPU counters are
shared between invocations, so **there is no sleep to take a CPU sample**. The
first invocation seeds that history and proceeds if memory permits. Only when a
threshold is exceeded does the wrapper sleep for a random 1–5 seconds and retry.

Lake uses a cached toolchain overlay to route compiler calls through the wrapper;
your installed toolchain stays untouched. Discovery probes skip throttling, and
the wrapper stays silent for compatibility with `lake build --iofail`.

This delays new compiler processes; it cannot cap running processes or guarantee
against OOM. CPU checks cover the host; memory checks include visible cgroup v2
limits. C compilation and linking are outside the throttle.

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
