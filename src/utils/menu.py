"""Interactive terminal menu helpers.

Provides standardized single-select and multi-select menus
based on simple_term_menu, matching the pvecli UX conventions.
"""

from simple_term_menu import TerminalMenu


def select_menu(items: list[str], title: str) -> int | None:
    """Show a single-select menu. Returns selected index or None if cancelled."""
    menu = TerminalMenu(
        items,
        title=title,
        menu_cursor="> ",
        menu_cursor_style=("fg_cyan", "bold"),
    )
    return menu.show()


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
        **kwargs,
    )
    sel = menu.show()
    if sel is None:
        return None
    return list(sel) if isinstance(sel, tuple) else [sel]
