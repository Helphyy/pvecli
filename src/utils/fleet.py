"""Run one command against several profiles.

`-p` keeps accepting a single profile name. It also accepts:
  - a list:     -p lab1,lab4
  - a pattern:  -p 'client-*'     (fnmatch, '*' for every profile)
  - a group:    -p @prod          (groups: {prod: [a, b]} in config.yaml)
and any mix of them, separated by commas.

Patterns and groups skip profiles marked `fleet: false`: those are only
reached when named explicitly, which is how a production profile stays out
of a broad `-p '*'`.

A command decorated with @fleet runs once per profile, in order, under a
header line. A failing profile does not stop the others; the command exits
1 at the end if any failed. With --json the per-profile documents are
gathered into a single one keyed by profile name.
"""

import fnmatch
import io
import json
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any

import typer

_PATTERN_CHARS = "*?["
# Profiles queried at the same time by read-only fleet commands
_MAX_PARALLEL = 8


def is_multi_profile(spec: str | None) -> bool:
    """Whether a -p value can designate more than one profile."""
    return bool(spec) and ("," in spec or spec.startswith("@") or any(c in spec for c in _PATTERN_CHARS))


def resolve_profiles(spec: str | None) -> list[str]:
    """Expand a -p value into an ordered list of profile names.

    Raises:
        ConfigError: on an unknown profile, group, or a pattern matching nothing.
    """
    from ..api.exceptions import ConfigError
    from ..config import ConfigManager

    config = ConfigManager().get()
    profiles = config.profiles
    if not spec:
        if not config.default_profile:
            raise ConfigError("No default profile set. Use --profile to specify one.")
        return [config.default_profile]

    def in_fleet(name: str) -> bool:
        return getattr(profiles[name], "fleet", True)

    names: list[str] = []
    for item in (x.strip() for x in spec.split(",")):
        if not item:
            continue
        if item.startswith("@"):
            group = config.groups.get(item[1:])
            if group is None:
                known = ", ".join(sorted(config.groups)) or "none defined"
                raise ConfigError(f"Unknown profile group '{item[1:]}'. Groups: {known}")
            unknown = [g for g in group if g not in profiles]
            if unknown:
                raise ConfigError(
                    f"Group '{item[1:]}' lists unknown profile(s): {', '.join(unknown)}"
                )
            names += [g for g in group if in_fleet(g)]
        elif any(c in item for c in _PATTERN_CHARS):
            matched = [p for p in profiles if fnmatch.fnmatchcase(p, item) and in_fleet(p)]
            if not matched:
                raise ConfigError(f"No profile matches '{item}'")
            names += matched
        else:
            if item not in profiles:
                raise ConfigError(
                    f"Profile '{item}' not found. Available profiles: {', '.join(profiles)}"
                )
            names.append(item)
    return list(dict.fromkeys(names))


def _run_one(func: Callable[..., Any], args: tuple, kwargs: dict) -> int:
    """Run the command for one profile and return its exit code."""
    try:
        func(*args, **kwargs)
        return 0
    except typer.Exit as e:
        return e.exit_code
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


class _ThreadStream:
    """sys.stdout/stderr stand-in sending each worker thread to its own buffer.

    Rich resolves sys.stdout on every write, so commands run in parallel
    threads write into separate buffers, printed in profile order once all
    are done. isatty() reports the real stream, so colors are kept.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self._local = threading.local()

    def bind(self, buffer: io.StringIO | None) -> None:
        self._local.buffer = buffer

    def _target(self) -> Any:
        return getattr(self._local, "buffer", None) or self._real

    def write(self, data: str) -> int:
        return self._target().write(data)

    def flush(self) -> None:
        self._target().flush()

    def isatty(self) -> bool:
        return self._real.isatty()

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8")


def _run_captured(func: Callable[..., Any], args: tuple, kwargs: dict,
                  out: _ThreadStream, err: _ThreadStream) -> tuple[int, str, str]:
    """Run one profile in the current thread, capturing what it prints."""
    o, e = io.StringIO(), io.StringIO()
    out.bind(o)
    err.bind(e)
    try:
        code = _run_one(func, args, kwargs)
    finally:
        out.bind(None)
        err.bind(None)
    return code, o.getvalue(), e.getvalue()


def fleet(func: Callable[..., Any] | None = None, *, parallel: bool = False) -> Any:
    """Let a command take several profiles in -p.

    Goes between @app.command(...) and @async_to_sync. The command keeps its
    signature; it only needs a `profile` parameter (and optionally
    `json_output`).

    parallel=True runs the profiles at the same time, for read-only commands
    that never prompt: an unreachable cluster then costs its timeout once,
    not once per profile. Commands that may ask for a confirmation stay
    sequential, so prompts never interleave.
    """
    if func is None:
        return lambda f: fleet(f, parallel=parallel)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        spec = kwargs.get("profile")
        if not is_multi_profile(spec):
            return func(*args, **kwargs)

        from ..api.exceptions import PVECliError
        from .output import console, emit_json, err_console, print_error

        try:
            names = resolve_profiles(spec)
        except PVECliError as e:
            print_error(str(e))
            raise typer.Exit(1)

        json_mode = bool(kwargs.get("json_output"))
        capture = parallel or json_mode
        results: dict[str, tuple[int, str, str]] = {}

        if capture:
            real_out, real_err = sys.stdout, sys.stderr
            out, err = _ThreadStream(real_out), _ThreadStream(real_err)
            sys.stdout, sys.stderr = out, err
            try:
                workers = min(_MAX_PARALLEL, len(names)) if parallel else 1
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        name: pool.submit(
                            _run_captured, func, args, {**kwargs, "profile": name}, out, err
                        )
                        for name in names
                    }
                    for name, future in futures.items():
                        results[name] = future.result()
            finally:
                sys.stdout, sys.stderr = real_out, real_err

        documents: dict[str, Any] = {}
        failed: list[str] = []
        for name in names:
            if capture:
                code, out_text, err_text = results[name]
            if json_mode:
                text = out_text.strip()
                try:
                    documents[name] = json.loads(text) if text else None
                except ValueError:
                    documents[name] = {"output": text}
                if code:
                    # Keep the reason with the profile it belongs to
                    documents[name] = {
                        "error": err_text.strip() or text or f"exit code {code}",
                        "exit_code": code,
                    }
            else:
                console.rule(f"[bold cyan]{name}[/bold cyan]", align="left")
                if capture:
                    sys.stdout.write(out_text)
                    sys.stderr.write(err_text)
                    sys.stdout.flush()
                else:
                    code = _run_one(func, args, {**kwargs, "profile": name})
                console.print()
            if code:
                failed.append(name)

        if json_mode:
            emit_json(documents)
        if failed:
            # stderr: a --json stream on stdout must stay parseable
            err_console.print(
                f"[yellow]Failed on {len(failed)}/{len(names)} profile(s): {', '.join(failed)}[/yellow]"
            )
            raise typer.Exit(1)
        return None

    return wrapper
