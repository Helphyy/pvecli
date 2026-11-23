"""Short options form one dictionary across the whole CLI.

Before November 2026, -s meant --storage, --state, --split, --swap, --status,
--shell, --scale or --source-file depending on the command, and --pool was
-pl in `vm add` but -po in `ct add`. Now:

- a short option names the same long option in every command;
- a long option that has a short form has the same one everywhere it exists.
"""

from collections import defaultdict

from typer.main import get_command

from src.cli.main import app


def _options():
    """Yield (command path, long names, short names) for every option."""

    def walk(cmd, path):
        for p in cmd.params:
            if getattr(p, "param_type_name", "") != "option":
                continue
            names = list(p.opts) + list(p.secondary_opts)
            longs = tuple(n for n in names if n.startswith("--"))
            shorts = tuple(n for n in names if not n.startswith("--"))
            yield " ".join(path) or "pvecli", longs, shorts
        for name, sub in (getattr(cmd, "commands", None) or {}).items():
            yield from walk(sub, path + [name])

    yield from walk(get_command(app), [])


OPTIONS = list(_options())


def test_the_tree_has_short_options():
    assert sum(1 for _, _, shorts in OPTIONS if shorts) > 400


def test_a_short_option_has_one_meaning():
    meanings = defaultdict(lambda: defaultdict(list))
    for path, longs, shorts in OPTIONS:
        for short in shorts:
            meanings[short][longs].append(path)
    clashes = {
        short: {"/".join(longs): paths[:3] for longs, paths in by_long.items()}
        for short, by_long in meanings.items()
        if len(by_long) > 1
    }
    assert not clashes, clashes


def test_a_long_option_has_one_short_form_everywhere():
    forms = defaultdict(lambda: defaultdict(list))
    for path, longs, shorts in OPTIONS:
        forms[longs][shorts].append(path)
    drifts = {
        "/".join(longs): {" ".join(shorts) or "(none)": paths[:3] for shorts, paths in by_short.items()}
        for longs, by_short in forms.items()
        if len(by_short) > 1 and any(by_short)
    }
    assert not drifts, drifts
