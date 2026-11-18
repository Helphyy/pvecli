"""config.yaml stays readable while several threads load it.

Loading a config holding a plain password encrypts it and writes the file
back. Fleet mode loads the config from several threads at once: a
non-atomic write let a thread read a truncated file and report a healthy
profile as missing ("Profile 'p1' not found. Available profiles: ").
"""

from concurrent.futures import ThreadPoolExecutor

import yaml

from src.config import ConfigManager


def test_parallel_loads_never_see_a_partial_file(home):
    cfg_file = home / ".config" / "pvecli" / "config.yaml"
    plain = cfg_file.read_text()
    for _ in range(20):
        # Back to a plain password each round: every load then rewrites
        cfg_file.write_text(plain)
        with ThreadPoolExecutor(max_workers=16) as pool:
            hosts = list(pool.map(lambda _: ConfigManager().get_profile("p1").host, range(32)))
        assert hosts == ["h1"] * 32


def test_save_leaves_no_temporary_file(home):
    cm = ConfigManager()
    cm.save(cm.get())
    files = [p.name for p in (home / ".config" / "pvecli").iterdir()]
    assert "config.yaml" in files
    assert not [f for f in files if f.endswith(".tmp")]
    assert yaml.safe_load((home / ".config" / "pvecli" / "config.yaml").read_text())["profiles"]["p1"]["host"] == "h1"
