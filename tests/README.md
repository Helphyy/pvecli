# Tests

```bash
pytest                          # static, unit and functional tests (no network)
pytest tests/unit               # one layer
pytest -k migrate               # one subject
PVECLI_LAB=1 PVECLI_LAB_PROFILES=lab1,lab9 pytest -m lab   # read-only, against a lab
```

`ruff check src tests` and `mypy src` complete the gate; the CI runs all three on every push.

## Layers

| Folder | What it protects | Network |
|:--|:--|:--|
| `static/` | Every command loads and answers `--help`, every command quoted in the docs exists, house rules (no long dashes, version consistent, no direct `click` import) | none |
| `unit/` | Pure helpers: parsing, matching, rules, grouping | none |
| `functional/` | The real CLI run against a fake Proxmox API: what is sent, what is printed, the exit code | none |
| `integration/` | Read-only commands against a real lab, opt-in | lab |

## The fake API

`tests/fake_api.py` replaces only the network layer of `ProxmoxClient`: every real client method runs, so a test goes through the same code as production. Routes are matched by method and path, optionally per profile host (several clusters at once):

```python
def test_start(cli, api):
    api.route("GET", "/cluster/resources", [{"vmid": 100, "type": "qemu", "node": "pve1", "status": "stopped"}])
    api.route("GET", "/nodes/{node}/qemu/{vmid}/status/current", {"status": "stopped"})
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/start", upid("pve1", "qmstart", 100))
    r = cli("vm", "start", "100", "-p", "p1")
    assert r.exit_code == 0
    assert api.called("POST", "/nodes/pve1/qemu/100/status/start")
```

A route can return a value, raise (`api.error(500, "...")`, `api.not_implemented()`, `api.forbidden("/", "Sys.Modify")`) or be a function of the call. `api.writes()` lists every non-GET call: a refusal test asserts it is empty.

The `clock` fixture makes `asyncio.sleep` return at once and advance `time.time()`: polling loops and their timeouts run instantly.

Every test runs with `HOME` in a temporary directory holding its own `config.yaml` (profiles `p1`, `p2`, `p3`, `prod`): the real profiles are never read.

## Fixtures are real responses

`tests/fixtures/pve-9.1/` and `pve-9.2/` hold responses recorded on a lab, secrets removed. A file containing `__error__` is how that version refuses the call (a missing endpoint, HA groups migrated to rules...). Refresh them on a new Proxmox release:

```bash
python -m tests.tools.record_fixtures <lab-profile> pve-9.3
```

Prefer a recorded response over a hand-written one. A hand-written fixture encodes what we believe Proxmox answers; several bugs came from exactly that belief (`not_allowed_nodes` lists every node with an empty entry, container addresses carry their prefix, `suspendall` always hibernates).

## Working test first

For a bug:

1. Write the test that reproduces it, with the expected behavior computed by hand from what Proxmox really does, never from what the code returns. Run it: it must fail.
2. Fix the code. The test passes.
3. Check the test protects anything: put the bug back for a moment, the test must fail again.

For a feature, start with the functional test of the command as a user would type it, then the unit tests of the helpers it needs.

A test that cannot fail is worse than no test: for every grouping or matching rule, also test that two different things are kept apart.
