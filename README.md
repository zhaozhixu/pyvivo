# pyvivo

`pyvivo` is a REPL-style development tool for inspecting and
modifying a running CPython process. It uses Python 3.14's PEP 768
`sys.remote_exec()` API to bootstrap a small in-process agent, then
communicates through a mode-`0600` Unix-domain socket.

The name is short for *in vivo* — Latin for "within the living", the life
sciences' term for an experiment done in a living organism, as opposed to
*in vitro* ("in glass"): in a test tube, on dead or isolated material.
pyvivo works in vivo: it inspects and modifies the live, running interpreter.

The workflow is borrowed from the Lisp REPL tradition, where development means
working on a live image — evaluating expressions and redefining functions in a
program that keeps running — rather than the edit–restart cycle.
pyvivo's architecture includes an agent inside the process, a controller outside,
a socket between them. It can inject the agent into an already-running process
that never planned for it, and redefine a function with a tracked patch
of one function's `__code__`, with rollback.

> This is a research prototype, not a production debugger. Attaching grants
> arbitrary code execution inside the target process.

## Requirements

- CPython 3.14 or newer, with remote debugging enabled.
- Unix-domain sockets. The current prototype targets Linux/macOS-style systems.
- Permission to trace the target process. On Linux this is governed by ptrace,
  Yama, process ancestry, and capabilities such as `CAP_SYS_PTRACE`. On macOS,
  taking the target's Mach task port generally requires running the controller
  as root (`sudo`), even when the target is the controller's own child.

## Install

`pyvivo` is a standard Python package with no runtime dependencies, so any
PEP 517 installer works. It ships a `pyvivo` console script, but every command
in this README is written as `python -m pyvivo` so it runs the same regardless
of how (or whether) you install it — from an active virtual environment they
are interchangeable.

With [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.14

# Install from PyPI
uv pip install pyvivo

# Install from source
uv sync
```

With pip:

```sh
python -m venv .venv && . .venv/bin/activate

# Install from PyPI
pip install pyvivo

# Install from source
pip install -e .
```

## Running the tests

The suite is standard-library only:

```sh
python -m unittest discover -s tests
```

## Examples

These drive pyvivo against the sample programs in `example/`.

### Attach to the example process

Terminal 1:

```sh
python example/target.py
```

Terminal 2 (`PID` is printed by the target, `sudo` may be required):

```sh
python -m pyvivo remote PID attach
python -m pyvivo remote PID eval 'counter'
python -m pyvivo remote PID patch __main__:compute example/patch_compute.py
python -m pyvivo remote PID rollback PATCH_ID
python -m pyvivo remote PID detach
```

Every command except `attach` automatically attaches if necessary. `detach`
does not attach merely to report that the process is already detached.

On macOS, attaching needs the target's Mach task port, which the OS grants only
to root — parent/child ancestry does not help either — so prefix these commands, and
the programmatic demos below, with `sudo`:

```sh
sudo python -m pyvivo remote PID attach
```

Keep using `sudo` for every follow-up command on the same target: the socket
path is derived from the controller's uid and temporary directory, so mixing
sudo and non-sudo invocations computes different paths.

### Process trees

pyvivo can recursively discover a root process and all its descendants, then run
one command against the whole tree and aggregate the per-process results. To try
it against the sample tree in `example/`:

Terminal 1 (a root plus two child workers; it prints the root pid):

```sh
python example/tree_target.py --children 2
#   tree root pid=ROOT children=[...]
```

Terminal 2 (`sudo` may be required):

```sh
python -m pyvivo tree ROOT list                        # discover the whole tree
python -m pyvivo tree ROOT --descendants-only stack
python -m pyvivo tree ROOT --descendants-only eval 'compute(3)'
python -m pyvivo tree ROOT --descendants-only patch __main__:compute example/patch_compute.py
python -m pyvivo tree ROOT --descendants-only rollback SHARED_PATCH_ID   # id printed by the patch above
python -m pyvivo tree ROOT --descendants-only detach --rollback
```

`--descendants-only` excludes the root; drop it to include the root too — the
sample tree is homogeneous, so every process (root included) exposes
`__main__:compute`. The same commands work for any local multi-process Python
application, not just this sample.

Tree discovery is framework-neutral: it uses OS parent/child relationships and
does not interpret application-specific environment variables. Discovery reads
`/proc` on Linux and falls back to `ps` on macOS/BSD. Each process has a
separate CPython interpreter, so the controller bootstraps one agent per PID
and communicates with each agent over its own Unix socket
`pyvivo-UID-PID.sock` in the controller's temporary directory. The command
aggregates their independent responses into one JSON result.

After a tree attach, each process remains independently addressable:

```sh
python -m pyvivo remote CHILD_PID stack
python -m pyvivo remote CHILD_PID eval 'some_process_specific_expression'
```

A tree patch generates one patch ID in the controller and supplies that same ID
to every process where the patch succeeds. The top-level tree response includes
the ID, so it can be used for a later tree-wide rollback:

```sh
python -m pyvivo tree ROOT_PID --descendants-only patch package:function patch.py
python -m pyvivo tree ROOT_PID --descendants-only rollback SHARED_PATCH_ID
```

Tree operations do not assume homogeneous programs. Each request is evaluated
against each process's own modules, threads, and frames. If one child runs a
different script, lacks a target function, has a shallower stack, or cannot
evaluate an expression, that child returns `ok: false` while other children may
return `ok: true`. Use `python -m pyvivo remote CHILD_PID ...` for
process-specific frame selection or probes.

### Programmatic demo

The `example/` directory has runnable demos that drive pyvivo's client API end
to end — spawn a target, attach, inspect, hot-patch, roll back, and detach:

```sh
python example/demo.py                     # single process
python example/tree_demo.py --children 2   # a process tree
```

`demo.py` starts one target as its child, attaches, lists threads and stack
frames, evaluates target state, replaces a function's `__code__`, rolls it back,
and detaches. `tree_demo.py` runs the same cycle across a whole process tree,
patching every process under one shared patch id and aggregating their results.
Both scripts read as a worked example of using `pyvivo.client` directly.

## Command reference

Usage lives in the CLI help, so it stays in sync with the code:

```sh
python -m pyvivo --help          # modes: remote, tree
python -m pyvivo remote --help   # ping, threads, stack, locals, eval, exec, patch, rollback, patches, detach
python -m pyvivo remote PID eval --help
python -m pyvivo tree --help
```

The `eval`/`exec`, `patch`, and `detach` help text documents the semantics
worth knowing before use — frame and name resolution, the call-shape and
free-variable requirements for a patch, and the detach policies.

## Important limitations

- Foreign-frame inspection is a concurrent snapshot; the selected thread is not
  paused.
- A CPython safe evaluation point is not necessarily an application safe point.
- `repr()` and arbitrary eval/exec may have side effects or significant cost.
- The socket has restrictive filesystem permissions but no complete peer
  authorization, audit, or production threat model.
- Patch history is memory-only and is not durable across process exit or
  `detach --keep-patches`.
