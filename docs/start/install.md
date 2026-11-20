---
icon: lucide/download
title: Install pvecli
description: Install pvecli with pipx or pip, check the version and turn on shell completion.
---

# Install pvecli

pvecli needs Python 3.10 or later on the machine you run it from. It installs from its Git repository.

## With pipx

[pipx](https://pipx.pypa.io/) puts pvecli in its own virtual environment and the `pvecli` command on your `PATH`:

```bash
pipx install git+https://github.com/Helphyy/pvecli.git
```

To update it later:

```bash
pipx upgrade pvecli
```

## With pip in a virtual environment

Without pipx, create a virtual environment for pvecli and install it there:

```bash
python3 -m venv ~/.local/share/pvecli
~/.local/share/pvecli/bin/pip install git+https://github.com/Helphyy/pvecli.git
mkdir -p ~/.local/bin
ln -s ~/.local/share/pvecli/bin/pvecli ~/.local/bin/pvecli
```

The link makes `pvecli` available wherever `~/.local/bin` is in your `PATH`. To update, run the same `pip install` with `--upgrade`.

## Check the installation

```bash
pvecli --version
```

```text
pvecli version 1.12.3
```

## Shell completion

Completion of commands and options works in bash, zsh, fish and PowerShell. pvecli detects your shell and installs it:

```bash
pvecli --install-completion
```

```text
zsh completion installed in /home/you/.zfunc/_pvecli
Completion will take effect once you restart the terminal
```

| Shell | Completion script | Startup file changed |
|:--|:--|:--|
| bash | `~/.bash_completions/pvecli.sh` | `~/.bashrc` |
| zsh | `~/.zfunc/_pvecli` | `~/.zshrc` |
| fish | `~/.config/fish/completions/pvecli.fish` | none |
| PowerShell | appended to your profile (`$PROFILE`) | `$PROFILE` |

In PowerShell, the install also sets the execution policy of the current user to `Unrestricted`.

Open a new terminal, then type `pvecli vm ` and press ++tab++.

To put the script somewhere else, print it with `pvecli --show-completion` and save it where your shell loads completions.
