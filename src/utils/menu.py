"""Interactive terminal menu helpers.

Provides standardized single-select and multi-select menus
based on simple_term_menu, matching the pvecli UX conventions.
"""

import os

from simple_term_menu import TerminalMenu  # type: ignore[import-untyped]  # no stubs nor py.typed, no types-simple-term-menu package exists


def _interactive() -> bool:
    """Whether a menu can be drawn at all.

    Probe the channel simple_term_menu actually uses: it opens /dev/tty
    directly and never touches sys.stdin or sys.stdout, so piping a command
    must keep the menu working. Without a controlling terminal the open fails
    and callers get a usable message instead of a traceback.
    """
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        return True
    except OSError:
        return False


def require_terminal() -> None:
    """Raise PVECliError when nothing interactive can be shown at all.

    For prompts that are not menus (plain text input), so they refuse with
    the same message as a menu instead of a bare "Aborted.".
    """
    if not _interactive():
        from ..api.exceptions import PVECliError

        raise PVECliError(
            "No terminal available for the interactive menu: pass the value as "
            "an argument or an option"
        )


def menu_row(modified: bool, label: str, value: str, width: int) -> str:
    """Build an edit-menu row with a star marker on modified fields.

    Entries must stay plain text: simple_term_menu truncates entries with
    raw slicing, so any ANSI escape code breaks the menu layout.
    """
    star = "* " if modified else "  "
    return f"{star}{label.ljust(width)}  {value}"


def select_menu(items: list[str], title: str) -> int | None:
    """Show a single-select menu. Returns selected index or None if cancelled.

    Raises PVECliError when there is no terminal at all, so callers report a
    missing argument instead of crashing.
    """
    # Raising lets every command turn this into its usual error path and
    # a non-zero exit code, instead of looking like a user cancellation.
    require_terminal()
    menu = TerminalMenu(
        items,
        title=title,
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("bg_cyan", "fg_black"),
    )
    return menu.show()


def reorder_menu(items: list[str], title: str) -> tuple[int | None, str | None]:
    """Show a menu that accepts u/d/r keys for reordering in addition to Enter.

    Returns (cursor_index, key) where key is one of 'enter', 'u', 'd', 'r', or None.
    """
    if not _interactive():
        return None, None
    menu = TerminalMenu(
        items,
        title=title,
        accept_keys=("enter", "u", "d", "r"),
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("bg_cyan", "fg_black"),
    )
    idx = menu.show()
    return idx, menu.chosen_accept_key


def multi_select_menu(
    items: list[str],
    title: str,
    preselected: list[int] | None = None,
) -> list[int] | None:
    """Show a multi-select menu. Returns list of selected indices or None if cancelled.

    Raises PVECliError when there is no terminal at all, like select_menu.
    """
    if not _interactive():
        from ..api.exceptions import PVECliError

        raise PVECliError(
            "No terminal available for the interactive menu: pass the values as "
            "options"
        )
    kwargs: dict = {}
    if preselected is not None:
        kwargs["preselected_entries"] = preselected
    menu = TerminalMenu(
        items,
        title=title,
        multi_select=True,
        show_multi_select_hint=True,
        show_multi_select_hint_text="Space: toggle | Enter: confirm | Escape: cancel",
        multi_select_select_on_accept=False,
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
        menu_highlight_style=("bg_cyan", "fg_black"),
        **kwargs,
    )
    sel = menu.show()
    if sel is None:
        return None
    return list(sel) if isinstance(sel, tuple) else [sel]
