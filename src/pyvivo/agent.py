"""Tiny in-process agent bootstrapped through Python 3.14 sys.remote_exec."""
from __future__ import annotations

import importlib
import difflib
import hashlib
import inspect
import json
import linecache
import marshal
import os
import socket
import sys
import threading
import traceback
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SERVER: socket.socket | None = None
_SERVER_THREAD: threading.Thread | None = None
_SOCKET_PATH: str | None = None
_PATCHES: dict[str, dict[str, Any]] = {}


def _safe_repr(value: Any, limit: int = 500) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover - defensive in a foreign process
        text = f"<repr failed: {type(exc).__name__}: {exc}>"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _frames() -> dict[int, types.FrameType]:
    return sys._current_frames()


def _thread_name(thread_id: int) -> str:
    for thread in threading.enumerate():
        if thread.ident == thread_id:
            return thread.name
    return "<unknown>"


def _select_thread(selector: str | int | None) -> tuple[int, types.FrameType]:
    frames = _frames()
    if selector in (None, "main"):
        main_id = threading.main_thread().ident
        if main_id is None or main_id not in frames:
            raise LookupError("main thread has no Python frame")
        return main_id, frames[main_id]

    thread_id = int(selector)
    try:
        return thread_id, frames[thread_id]
    except KeyError as exc:
        raise LookupError(f"thread {thread_id} has no Python frame") from exc


def _select_frame(selector: str | int | None, depth: int) -> tuple[int, types.FrameType]:
    thread_id, frame = _select_thread(selector)
    for _ in range(depth):
        if frame.f_back is None:
            raise LookupError(f"frame depth {depth} is outside the stack")
        frame = frame.f_back
    return thread_id, frame


def _frame_info(frame: types.FrameType, depth: int) -> dict[str, Any]:
    filename = frame.f_code.co_filename
    line_number = frame.f_lineno
    result = {
        "depth": depth,
        "file": filename,
        "line": line_number,
        "function": frame.f_code.co_name,
        "module": frame.f_globals.get("__name__", "?"),
    }
    source = linecache.getline(filename, line_number).strip()
    if source:
        result["source"] = source
    return result


def _threads(_: dict[str, Any]) -> dict[str, Any]:
    result = []
    main_id = threading.main_thread().ident
    for thread_id, frame in _frames().items():
        result.append(
            {
                "id": thread_id,
                "name": _thread_name(thread_id),
                "main": thread_id == main_id,
                "top": _frame_info(frame, 0),
            }
        )
    result.sort(key=lambda item: (not item["main"], item["name"], item["id"]))
    return {"threads": result}


def _stack(request: dict[str, Any]) -> dict[str, Any]:
    thread_id, frame = _select_thread(request.get("thread"))
    stack = []
    depth = 0
    while frame is not None:
        stack.append(_frame_info(frame, depth))
        frame = frame.f_back
        depth += 1
    return {"thread": thread_id, "name": _thread_name(thread_id), "stack": stack}


def _locals(request: dict[str, Any]) -> dict[str, Any]:
    thread_id, frame = _select_frame(request.get("thread"), int(request.get("frame", 0)))
    values = {name: _safe_repr(value) for name, value in frame.f_locals.items()}
    return {
        "thread": thread_id,
        "frame": _frame_info(frame, int(request.get("frame", 0))),
        "locals": values,
    }


def _eval(request: dict[str, Any]) -> dict[str, Any]:
    thread_id, frame = _select_frame(request.get("thread"), int(request.get("frame", 0)))
    filename = request.get("filename", "<pyvivo eval>")
    code = compile(request["code"], filename, "eval")
    value = eval(code, frame.f_globals, frame.f_locals)
    return {
        "thread": thread_id,
        "frame": _frame_info(frame, int(request.get("frame", 0))),
        "source_file": filename,
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "repr": _safe_repr(value, limit=4000),
    }


def _exec(request: dict[str, Any]) -> dict[str, Any]:
    thread_id, frame = _select_frame(request.get("thread"), int(request.get("frame", 0)))
    filename = request.get("filename", "<pyvivo exec>")
    code = compile(request["code"], filename, "exec")
    exec(code, frame.f_globals, frame.f_locals)
    return {
        "thread": thread_id,
        "frame": _frame_info(frame, int(request.get("frame", 0))),
        "source_file": filename,
        "status": "executed",
    }


def _resolve_target(target: str) -> tuple[Any, str, types.FunctionType]:
    if ":" not in target:
        raise ValueError("target must be MODULE:ATTRIBUTE[.ATTRIBUTE...]")
    module_name, attr_path = target.split(":", 1)
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)

    owner: Any = module
    parts = attr_path.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    attr = parts[-1]
    function = inspect.getattr_static(owner, attr)
    if isinstance(function, (staticmethod, classmethod)):
        function = function.__func__
    if not isinstance(function, types.FunctionType):
        raise TypeError(
            f"{target} resolved to {type(function).__name__}, not a Python function"
        )
    return owner, attr, function


def _code_hash(code: types.CodeType) -> str:
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


def _source_hash(source: str | None) -> str | None:
    if source is None:
        return None
    return hashlib.sha256(source.encode()).hexdigest()


def _function_source(function: types.FunctionType) -> str | None:
    # If this function is already patched, inspect cannot resolve a synthetic
    # or controller-local filename in the target. Reuse the source we recorded.
    for patch in reversed(list(_PATCHES.values())):
        if patch["function"] is function and function.__code__ is patch["new_code"]:
            return patch["new_source"]
    try:
        return inspect.getsource(function)
    except (OSError, TypeError):
        return None


def _source_diff(old_source: str | None, new_source: str, target: str) -> str | None:
    if old_source is None:
        return None
    return "".join(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"{target} (before)",
            tofile=f"{target} (after)",
        )
    )


def _patch(request: dict[str, Any]) -> dict[str, Any]:
    target = request["target"]
    owner, attr, function = _resolve_target(target)
    old_source = _function_source(function)
    new_source = request["source"]
    source_file = request.get("filename", f"<pyvivo patch {target}>")
    namespace: dict[str, Any] = {}
    exec(compile(new_source, source_file, "exec"), function.__globals__, namespace)
    replacement = namespace.get(function.__name__)
    if not isinstance(replacement, types.FunctionType):
        raise ValueError(f"source must define a function named {function.__name__!r}")
    if function.__code__.co_freevars != replacement.__code__.co_freevars:
        raise ValueError(
            "replacement has incompatible free variables: "
            f"{replacement.__code__.co_freevars!r} != {function.__code__.co_freevars!r}"
        )
    old_shape = (
        function.__code__.co_argcount,
        function.__code__.co_posonlyargcount,
        function.__code__.co_kwonlyargcount,
        bool(function.__code__.co_flags & inspect.CO_VARARGS),
        bool(function.__code__.co_flags & inspect.CO_VARKEYWORDS),
    )
    new_shape = (
        replacement.__code__.co_argcount,
        replacement.__code__.co_posonlyargcount,
        replacement.__code__.co_kwonlyargcount,
        bool(replacement.__code__.co_flags & inspect.CO_VARARGS),
        bool(replacement.__code__.co_flags & inspect.CO_VARKEYWORDS),
    )
    if old_shape != new_shape:
        raise ValueError(f"replacement has an incompatible call shape: {new_shape} != {old_shape}")

    patch_id = request.get("patch_id") or uuid.uuid4().hex[:12]
    if not isinstance(patch_id, str) or not patch_id:
        raise ValueError("patch_id must be a non-empty string")
    if patch_id in _PATCHES:
        raise ValueError(f"patch id already exists: {patch_id}")
    patch = {
        "patch_id": patch_id,
        "target": target,
        "function": function,
        "old_code": function.__code__,
        "new_code": replacement.__code__,
        "old_source": old_source,
        "new_source": new_source,
        "source_file": source_file,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "old_signature": str(inspect.signature(function)),
        "new_signature": str(inspect.signature(replacement)),
        "diff": _source_diff(old_source, new_source, target),
    }
    _PATCHES[patch_id] = patch
    function.__code__ = replacement.__code__
    return _patch_info(patch, include_source=True)


def _code_info(code: types.CodeType) -> dict[str, Any]:
    return {
        "name": code.co_name,
        "qualname": code.co_qualname,
        "file": code.co_filename,
        "first_line": code.co_firstlineno,
        "argcount": code.co_argcount,
        "positional_only": code.co_posonlyargcount,
        "keyword_only": code.co_kwonlyargcount,
        "freevars": list(code.co_freevars),
        "hash": _code_hash(code),
    }


def _patch_info(patch: dict[str, Any], *, include_source: bool) -> dict[str, Any]:
    function = patch["function"]
    result = {
        "patch_id": patch["patch_id"],
        "target": patch["target"],
        "active": function.__code__ is patch["new_code"],
        "installed_at": patch["installed_at"],
        "source_file": patch["source_file"],
        "module": function.__module__,
        "qualname": function.__qualname__,
        "old_signature": patch["old_signature"],
        "new_signature": patch["new_signature"],
        "old": _code_info(patch["old_code"]),
        "new": _code_info(patch["new_code"]),
        "old_source_hash": _source_hash(patch["old_source"]),
        "new_source_hash": _source_hash(patch["new_source"]),
    }
    if include_source:
        result.update(
            {
                "old_source": patch["old_source"],
                "new_source": patch["new_source"],
                "diff": patch["diff"],
            }
        )
    return result


def _rollback(request: dict[str, Any]) -> dict[str, Any]:
    patch_id = request["patch_id"]
    try:
        patch = _PATCHES.pop(patch_id)
    except KeyError as exc:
        raise LookupError(f"unknown patch {patch_id}") from exc
    function = patch["function"]
    if function.__code__ is not patch["new_code"]:
        _PATCHES[patch_id] = patch
        raise RuntimeError("target changed since this patch was installed; refusing rollback")
    function.__code__ = patch["old_code"]
    return {"patch_id": patch_id, "target": patch["target"], "status": "rolled back"}


def _patches(request: dict[str, Any]) -> dict[str, Any]:
    patch_id = request.get("patch_id")
    include_source = request.get("full", False)
    if patch_id is not None:
        try:
            patches = [_PATCHES[patch_id]]
        except KeyError as exc:
            raise LookupError(f"unknown patch {patch_id}") from exc
    else:
        patches = list(_PATCHES.values())
    return {
        "storage": "target-process memory only",
        "patches": [
            _patch_info(patch, include_source=include_source) for patch in patches
        ],
    }


def _validate_rollback_all() -> list[dict[str, Any]]:
    patches = list(reversed(list(_PATCHES.values())))
    expected: dict[types.FunctionType, types.CodeType] = {}
    for patch in patches:
        function = patch["function"]
        current = expected.get(function, function.__code__)
        if current is not patch["new_code"]:
            raise RuntimeError(
                f"{patch['target']} changed outside the tracked patch chain; "
                "refusing detach rollback"
            )
        expected[function] = patch["old_code"]
    return patches


def _rollback_all() -> list[str]:
    patches = _validate_rollback_all()
    rolled_back = []
    for patch in patches:
        patch["function"].__code__ = patch["old_code"]
        rolled_back.append(patch["patch_id"])
    _PATCHES.clear()
    return rolled_back


def _detach(request: dict[str, Any]) -> dict[str, Any]:
    policy = request.get("policy", "refuse")
    if policy not in ("refuse", "rollback", "keep"):
        raise ValueError(f"unknown detach policy: {policy!r}")

    tracked = list(_PATCHES)
    rolled_back: list[str] = []
    if tracked and policy == "refuse":
        raise RuntimeError(
            f"cannot detach with {len(tracked)} tracked patch(es); "
            "use --rollback or --keep-patches"
        )
    if policy == "rollback":
        rolled_back = _rollback_all()
    elif policy == "keep":
        # The code remains modified, but releasing these records also releases
        # the only old-code references owned by this agent.
        _PATCHES.clear()

    return {
        "status": "detaching",
        "policy": policy,
        "tracked_patches": tracked,
        "rolled_back": rolled_back,
        "warning": (
            "patches remain installed; their rollback state is being discarded"
            if tracked and policy == "keep"
            else None
        ),
    }


_HANDLERS = {
    "ping": lambda _: {"pid": os.getpid(), "python": sys.version, "socket": _SOCKET_PATH},
    "threads": _threads,
    "stack": _stack,
    "locals": _locals,
    "eval": _eval,
    "exec": _exec,
    "patch": _patch,
    "rollback": _rollback,
    "patches": _patches,
    "detach": _detach,
}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("op")
    if operation not in _HANDLERS:
        raise ValueError(f"unknown operation: {operation!r}")
    return _HANDLERS[operation](request)


def _handle_connection(connection: socket.socket) -> None:
    with connection:
        reader = connection.makefile("rb")
        writer = connection.makefile("wb")
        for raw_line in reader:
            detach_requested = False
            try:
                request = json.loads(raw_line)
                detach_requested = request.get("op") == "detach"
                response = {"ok": True, "result": _dispatch(request)}
            except BaseException as exc:
                response = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            writer.write(json.dumps(response, ensure_ascii=True).encode() + b"\n")
            writer.flush()
            if detach_requested and response["ok"]:
                _shutdown_agent()
                return


def _serve(server: socket.socket) -> None:
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        threading.Thread(
            target=_handle_connection,
            args=(connection,),
            name="pyvivo-client",
            daemon=True,
        ).start()


def _shutdown_agent() -> None:
    global _SERVER, _SERVER_THREAD, _SOCKET_PATH

    server = _SERVER
    path = _SOCKET_PATH
    _SERVER = None
    _SERVER_THREAD = None
    _SOCKET_PATH = None
    if server is not None:
        try:
            server.close()
        except OSError:
            pass
    if path is not None:
        Path(path).unlink(missing_ok=True)


def install(socket_path: str) -> None:
    """Install one agent per process. Safe to call repeatedly."""
    global _SERVER, _SERVER_THREAD, _SOCKET_PATH

    if _SERVER is not None:
        return

    path = Path(socket_path)
    if path.exists():
        path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(4)

    _SERVER = server
    _SOCKET_PATH = str(path)
    _SERVER_THREAD = threading.Thread(
        target=_serve,
        args=(server,),
        name="pyvivo-agent",
        daemon=True,
    )
    _SERVER_THREAD.start()
