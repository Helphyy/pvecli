"""Interactive terminal menu helpers.

Provides standardized single-select and multi-select menus
based on simple_term_menu, matching the pvecli UX conventions.
"""

from simple_term_menu import TerminalMenu

def menu_row(modified: bool, label: str, value: str, width: int) -> str:
    """Build an edit-menu row with a star marker on modified fields.

    Entries must stay plain text: simple_term_menu truncates entries with
    raw slicing, so any ANSI escape code breaks the menu layout.
    """
    star = "* " if modified else "  "
    return f"{star}{label.ljust(width)}  {value}"


def select_menu(items: list[str], title: str) -> int | None:
    """Show a single-select menu. Returns selected index or None if cancelled."""
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
    """Show a multi-select menu. Returns list of selected indices or None if cancelled."""
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
