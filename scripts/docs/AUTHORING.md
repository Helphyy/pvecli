# Writing the pvecli documentation

The site is built by Zensical (the successor of Material for MkDocs) from
`docs/`, configured in `zensical.toml`, with the classic Material look and
the colors of Proxmox VE. The menu and the command reference (`docs/cli/`,
one page per command, generated from the code) come from
`python -m scripts.docs.gen_reference`; the written pages and their place in
the menu are listed in `SECTIONS` of `scripts/docs/gen_reference.py`.

## Tone

Plain technical documentation, like the docs of NetExec, Exegol or the
Proxmox VE admin guide. The reader came to do something: tell them how.

- English. Short sentences, present tense, second person ("you").
- Headings name the task or the thing ("Restore a backup", "Options", "If
  the node does not come back"). No slogans, no rhetorical headings, no
  questions as headings.
- No marketing words: never "simply", "just", "easily", "seamless",
  "powerful", "robust", "effortless", "modern", "blazing".
- Never the long dashes (U+2014, U+2013): use a comma, a colon, parentheses
  or a full stop.
- No padding: no intro that repeats the title, no closing summary, no
  "In this guide you will learn". One or two sentences, then the command.
- Not every page needs the same sections. Write what the task needs.
- Prefer a command and its output to a paragraph that describes it.
- Every claim is true: read the command's code in `src/cli/` and run it on
  the lab before writing it. If you did not see it in the code or on the
  lab, do not write it.

## Markdown

What `docs/sample.md` shows, nothing hand-styled beyond it:

- Commands to type: fenced ```` ```bash ```` blocks. A block followed by its
  capture repeats nothing: the capture shows the command, so do not put the
  same command in a block above it.
- Admonitions, sparingly: `!!! warning` (risk), `!!! danger` (data loss or
  downtime only), `!!! note`, `!!! tip`, `??? question` (collapsed, for a
  side question). Never two in a row.
- Content tabs when the same thing differs: `=== "PVE 9"` / `=== "PVE 8"`.
- Code annotations for config files: `# (1)!` in the block, then a numbered
  list right after it.
- Tables for options, comparisons and mappings.
- A numbered list for a procedure, a task list (`- [x]`) for a checklist.
- Section index pages: a `grid cards` block, one card per page of the
  section (Lucide icon, bold title, `---`, one line, an arrow link), as in
  `docs/sample.md`. The icon is the one of the page's front matter.
- Icons: Lucide only (`:lucide-server:`), in cards. Not in running text.
- Diagrams (Mermaid), when a picture explains what text cannot (a sequence,
  a flow, what talks to what). Most pages have none, none has more than two.
    - A flow or the states of something: colored with `classDef` (blue
      `#0078cf` checks, orange `#e57000` actions, green `#2f9e44` done, red
      `#e03131` stop or failure).
    - A sequence: `autonumber`, phases in `rect` bands with a `note`.
    - The big picture of a section or a page (what talks to what): a sketch,
      a hand-drawn diagram like Excalidraw: the block starts with
      `config: look: handDrawn`, inside `<div class="sketch" markdown>`, nodes
      colored by kind (workstation `#ffae0b`, node `#a5d8ff`, VM `#b2f2bb`,
      container `#d0bfff`, storage `#ffc9c9`). Name a classDef `host`, never
      `node` (the theme overrides it). If a subgraph title is cut, end it
      with `#nbsp;`.
- Buttons only on index pages, for the next step.
- Keyboard keys: `++ctrl+c++`.
- Video (`<video>`) only for what is not a terminal. A terminal is a
  capture or a recording.
- No emoji, no banners, no raw HTML beyond the includes, sketches and
  video above.

Each page starts with front matter (`icon: lucide/NAME`, `title`,
one-sentence `description`), then `# Title` (the title of the page in
`SECTIONS`). The icon must exist in the theme
(`.venv/lib/python3.14/site-packages/zensical_ui/templates/.icons/lucide/`).
Link the command reference pages you use, e.g.
`[backup restore](../cli/backup/restore.md)`.

## Terminal captures

Captures are real pvecli runs on the lab, anonymized, saved as HTML
fragments, shown as a dark code block with the real colors.

1. Add a `Shot` to `scripts/docs/shots/<section>.py` (create it):
   `from scripts.docs.screens import Shot`, then `SHOTS = [...]`. Name it
   `<section>-<what>`. Fields: `args` (with `-p {profile}`), `caption` (what
   it shows, for the authors), `stdin` and `echo` to answer a prompt,
   `marks` (texts the output must contain, else the shot fails),
   `width=100` for wide tables, `manual=True` when it needs guests set up
   first or changes the lab.
2. Run only your shots: `.venv/bin/python -m scripts.docs.screens NAME [NAME...]`.
3. Include it: `--8<-- "screens/NAME.html"`. To explain parts of the
   output, a short list after it that quotes what it explains.

Interactive menus: a `Cast` in the same file (`from scripts.docs.casts import
Cast, UP, DOWN, ENTER`; keys: `UP`, `DOWN`, `ENTER`, `SPACE`, text;
`manual=True` as for a shot), run
`.venv/bin/python -m scripts.docs.casts NAME`, then embed:

```html
<div class="cast" data-src="../../assets/casts/NAME.cast" data-poster="npt:0:3"></div>
```

`data-poster` is the time of the frame shown before it plays (pick one where
the menu is visible); `data-rows="N"` cuts the empty rows below the content.
A recording plays when it comes into view and has a seek bar.

(the path is relative to the built page: one `../` per folder level of the
page, plus one).

Anonymization: every API answer goes through the replacements of
`scripts/docs/screens.local.toml` (git-ignored). When a capture shows a
real lab name (node, VM, storage, IP, cluster, pool...), append it, never
rewrite the file: `echo '"real-name" = "doc-name"' >> scripts/docs/screens.local.toml`.
Documentation names: clusters `prod` and `staging`, nodes `pve1`..`pve6`,
VMs `web-01`, `db-01`, addresses `10.0.0.0/24`, `10.1.0.0/24`.

Output that cannot be produced on the lab (an interruption, a failure) is
written by hand in a plain ```` ```text ```` block.

## The lab

Captures run against your own test cluster, configured in
`scripts/docs/screens.local.toml`. Always pass `-p` explicitly, never point
a shot at a production cluster, never capture `pvecli config list` or
`config info` (they print your profiles). Capture the plan of anything that
takes the cluster down and answer `n`. Machine-specific rules are in
`scripts/docs/LAB.local.md` (git-ignored): read it before running anything.

## Build and check

Preview: `.venv/bin/zensical serve`. Several people building at once each
use their own folder:

```bash
sed 's|^\[project\]|[project]\nsite_dir = ".site-NAME"|' zensical.toml > .zensical-NAME.toml
.venv/bin/zensical build -f .zensical-NAME.toml 2>&1 | tail -3   # want: No issues found
```

Diagrams are drawn in the browser in a preview. The published site has them
drawn at build time, so they are there when the page loads: CI runs
`python -m scripts.docs.prerender_diagrams site` after the build (needs
`pip install -e ".[docs]"` and `python -m playwright install chromium`). Run
it on your own folder to see the published result:
`.venv/bin/python -m scripts.docs.prerender_diagrams .site-NAME`.

To preview the site, serve the folder with
`python3 -m http.server PORT --directory <absolute path>`, never from inside
it: each build replaces the folder.

After adding or renaming a page, or changing a command or its help, run
`.venv/bin/python -m scripts.docs.gen_reference` (CI runs it with `--check`).
