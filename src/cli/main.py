"""Main CLI application."""

import typer
from rich.console import Console

from .. import __version__
from . import cluster, config, ct, node, pool, storage, tag, vm

console = Console()

app = typer.Typer(
    name="pvecli",
    help="Modern CLI for Proxmox VE API",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(config.app, name="config")
app.add_typer(node.app, name="node")
app.add_typer(vm.app, name="vm")
app.add_typer(ct.app, name="ct")
app.add_typer(storage.app, name="storage")
app.add_typer(cluster.app, name="cluster")
app.add_typer(pool.app, name="pool")
app.add_typer(tag.app, name="tag")


def version_callback(value: bool) -> None:
    """Print version and exit.

    Args:
        value: Whether version flag was set
    """
    if value:
        console.print(f"pvecli version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        "-v",
        help="Show version and exit",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """pvecli - Modern CLI for Proxmox VE API.

    Manage your Proxmox Virtual Environment from the command line with an
    intuitive, powerful interface.

    Get started:
        pvecli config add     # Set up your first profile
        pvecli node list      # List cluster nodes
        pvecli --help         # Show all available commands
    """
    pass


if __name__ == "__main__":
    app()
