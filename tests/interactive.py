"""Scripted answers for interactive commands (menus, prompts, confirmations).

    def test_wizard(cli, api, ui):
        ui.menu("pve1")            # select_menu: pick the item containing "pve1"
        ui.multi(["iso", 2])       # multi_select_menu: by label or index
        ui.text("web01")           # prompt / menu_prompt / Prompt.ask / getpass
        ui.confirm(True)           # confirm / Confirm.ask
        ui.number(2)               # IntPrompt.ask
        r = cli("vm", "add", "-p", "p1")
        assert ui.asked  # every question shown, in order

Answers are consumed in order, per kind. A question with no answer left
fails the test and names the question: a wizard never asks something the
test did not see. `None` cancels (Escape in a menu, empty text).
"""

from __future__ import annotations

import sys
from collections import deque
from typing import Any


class UnexpectedQuestion(AssertionError):
    pass


class UI:
    def __init__(self) -> None:
        self.queues: dict[str, deque[Any]] = {k: deque() for k in ("menu", "multi", "text", "confirm", "number")}
        self.asked: list[tuple[str, str]] = []

    # ── scripting ────────────────────────────────────────────────────

    def menu(self, *answers: Any) -> UI:
        self.queues["menu"].extend(answers)
        return self

    def multi(self, *answers: Any) -> UI:
        self.queues["multi"].extend(answers)
        return self

    def text(self, *answers: Any) -> UI:
        self.queues["text"].extend(answers)
        return self

    def confirm(self, *answers: bool) -> UI:
        self.queues["confirm"].extend(answers)
        return self

    def number(self, *answers: int) -> UI:
        self.queues["number"].extend(answers)
        return self

    def left(self) -> dict[str, list[Any]]:
        """Answers never asked for: a test usually expects none."""
        return {k: list(q) for k, q in self.queues.items() if q}

    # ── answering ────────────────────────────────────────────────────

    def _next(self, kind: str, question: str) -> Any:
        self.asked.append((kind, question))
        if not self.queues[kind]:
            raise UnexpectedQuestion(f"no scripted {kind} answer for: {question!r}")
        return self.queues[kind].popleft()

    @staticmethod
    def _index(items: list[str], answer: Any, question: str) -> int:
        if isinstance(answer, int):
            return answer
        hits = [i for i, item in enumerate(items) if str(answer) in str(item)]
        if not hits:
            raise UnexpectedQuestion(f"{answer!r} is not offered in {question!r}: {items}")
        return hits[0]

    def select_menu(self, items: list[str], title: str = "", *a: Any, **k: Any) -> int | None:
        answer = self._next("menu", f"{title} {items}")
        return None if answer is None else self._index(items, answer, title)

    def multi_select_menu(self, items: list[str], title: str = "", *a: Any, **k: Any) -> list[int] | None:
        answer = self._next("multi", f"{title} {items}")
        return None if answer is None else [self._index(items, x, title) for x in answer]

    def text_answer(self, message: Any = "", *a: Any, default: Any = None, **k: Any) -> str:
        answer = self._next("text", str(message))
        if answer is None:
            return "" if default is None else str(default)
        return str(answer)

    def confirm_answer(self, message: Any = "", *a: Any, **k: Any) -> bool:
        return bool(self._next("confirm", str(message)))

    def number_answer(self, message: Any = "", *a: Any, default: Any = None, **k: Any) -> int:
        answer = self._next("number", str(message))
        return int(default if answer is None else answer)


PATCHED_NAMES = {
    "select_menu": "select_menu",
    "multi_select_menu": "multi_select_menu",
    "menu_prompt": "text_answer",
    "prompt": "text_answer",
    "prompt_vlan": "text_answer",
    "getpass": "text_answer",
    "confirm": "confirm_answer",
}


def install(monkeypatch: Any, ui: UI) -> None:
    """Route every interactive entry point of src.* to the script."""
    import rich.prompt

    from src.utils import menu

    monkeypatch.setattr(menu, "_interactive", lambda: True)
    for name, module in list(sys.modules.items()):
        if not name.startswith("src.") or module is None:
            continue
        for attr, method in PATCHED_NAMES.items():
            if hasattr(module, attr) and callable(getattr(module, attr)):
                monkeypatch.setattr(module, attr, getattr(ui, method))
    monkeypatch.setattr(rich.prompt.Prompt, "ask", classmethod(lambda cls, *a, **k: ui.text_answer(*a, **k)))
    monkeypatch.setattr(rich.prompt.Confirm, "ask", classmethod(lambda cls, *a, **k: ui.confirm_answer(*a, **k)))
    monkeypatch.setattr(rich.prompt.IntPrompt, "ask", classmethod(lambda cls, *a, **k: ui.number_answer(*a, **k)))
