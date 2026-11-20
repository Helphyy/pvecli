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
import re
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any

import typer

# Color codes of an error printed to a terminal: not part of a JSON value
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_PATTERN_CHARS = "*?["
# Profiles queried at the same time by read-only fleet commands
_MAX_PARALLEL = 8


def is_multi_profile(spec: str | None) -> bool:
    """Whether a -p value can designate more than one profile."""
    return bool(spec and ("," in spec or spec.startswith("@") or any(c in spec for c in _PATTERN_CHARS)))


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
            members = [g for g in group if in_fleet(g)]
            if not members:
                raise ConfigError(
                    f"Group '{item[1:]}' only holds profiles marked fleet: false; "
                    "name them explicitly to reach them"
                )
            names += members
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


def _cluster_identity(name: str) -> tuple | str:
    """What a profile really points at: cluster name and members, or the
    node itself for a standalone one. On failure, the error text."""
    import asyncio

    from ..api.client import ProxmoxClient
    from ..config import ConfigManager

    async def probe() -> tuple | str:
        try:
            async with ProxmoxClient(ConfigManager().get_profile(name)) as client:
                status = await client.get_cluster_status() or []
        except Exception as e:
            return str(e) or type(e).__name__
        cluster = next((x for x in status if x.get("type") == "cluster"), None)
        nodes = tuple(sorted(x.get("name", "") for x in status if x.get("type") == "node"))
        if cluster:
            return ("cluster", cluster.get("name"), nodes)
        # A standalone node: its name alone is not enough, every fresh
        # install is called "pve". Its address tells two of them apart.
        ips = tuple(sorted(str(x.get("ip", "")) for x in status if x.get("type") == "node"))
        return ("node", nodes, ips)

    return asyncio.run(probe())


def dedupe_by_cluster(names: list[str]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Keep one profile per cluster.

    Returns (kept, {skipped: kept_twin}, {unreachable: error}). Two profiles
    on the same cluster would make a command act twice on it (twice the same
    exec, twice the same snapshot). An unreachable profile is reported with
    the probe error instead of being tried a second time.
    """
    with ThreadPoolExecutor(max_workers=max(1, min(_MAX_PARALLEL, len(names)))) as pool:
        identities = dict(zip(names, pool.map(_cluster_identity, names), strict=True))
    kept: list[str] = []
    skipped: dict[str, str] = {}
    unreachable: dict[str, str] = {}
    first: dict[tuple, str] = {}
    for n in names:
        ident = identities[n]
        if isinstance(ident, str):
            unreachable[n] = ident
        elif ident in first:
            skipped[n] = first[ident]
        else:
            first[ident] = n
            kept.append(n)
    return kept, skipped, unreachable


def _fleet_exit_code(codes: list[int]) -> int:
    """One exit code for several profiles, from what failed on each.

    130 if one was interrupted, 1 if one could not run, else 2 when every
    failure was a finding (audit FAIL): a script can still tell them apart.
    """
    if 130 in codes:
        return 130
    if any(c not in (0, 2) for c in codes):
        return 1
    return 2 if 2 in codes else 0


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
        all_names = names
        names, twins, unreachable = dedupe_by_cluster(names)
        for skipped, kept in twins.items():
            # stderr: keeps a --json stream on stdout parseable
            err_console.print(
                f"[yellow]Skipping profile {skipped}: same cluster as {kept}[/yellow]"
            )

        json_mode = bool(kwargs.get("json_output"))
        capture = parallel or json_mode
        results: dict[str, tuple[int, str, str]] = {
            n: (1, "", f"Error: {err}\n") for n, err in unreachable.items()
        }

        if capture:
            real_out, real_err = sys.stdout, sys.stderr
            out, err = _ThreadStream(real_out), _ThreadStream(real_err)
            sys.stdout, sys.stderr = out, err
            try:
                workers = max(1, min(_MAX_PARALLEL, len(names))) if parallel else 1
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
        codes: list[int] = []
        ordered = [n for n in all_names if n in results or n in names]
        for name in ordered:
            if name in unreachable and not capture:
                console.rule(f"[bold cyan]{name}[/bold cyan]", align="left")
                print_error(unreachable[name])
                console.print()
                failed.append(name)
                codes.append(1)
                continue
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
                        "error": _ANSI.sub("", err_text).strip() or text or f"exit code {code}",
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
                codes.append(code)

        if json_mode:
            emit_json(documents)
        if failed:
            # stderr: a --json stream on stdout must stay parseable
            err_console.print(
                f"[yellow]Failed on {len(failed)}/{len(ordered)} profile(s): {', '.join(failed)}[/yellow]"
            )
            raise typer.Exit(_fleet_exit_code(codes))
        return None

    return wrapper
