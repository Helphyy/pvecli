"""The examples of the docstrings stay out of --help."""

import inspect

from typer.testing import CliRunner

from src.cli.find import find
from src.cli.main import _drop_examples, app


def test_help_has_no_examples():
    _drop_examples(app)
    for cmd in (["find"], ["backup", "list"], ["deploy"], ["vm", "deploy"]):
        out = CliRunner().invoke(app, [*cmd, "--help"]).output
        assert "Usage" in out
        assert "Examples" not in out


def test_docstrings_keep_them_for_the_reference():
    _drop_examples(app)
    assert "Examples:" in inspect.getdoc(find)
    help_text = next(c.help for c in app.registered_commands if c.callback is find)
    assert help_text.startswith("Find guests")
