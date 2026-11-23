"""A Proxmox VE firewall with state, behind the fake API.

It does what PVE 9.1 was seen doing on the lab (2026-11-23), quirks
included, so the tests of the write commands check the end state rather
than a list of calls:

- a new rule always goes first: `pos` is ignored on creation;
- `moveto` puts a rule before the one at that position, counted before
  the move;
- one digest per list (options, rules, aliases, the IP sets, each IP set,
  the groups, each group); a stale digest is refused;
- renaming an alias or an IP set leaves the references behind, renaming
  a security group carries the rules that insert it along;
- an IP set with entries is removed with force=1 only, a group empty only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from src.api.exceptions import APIError
from tests.fake_api import Call, FakeAPI


def _digest(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Level:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.rules: list[dict[str, Any]] = []
        self.aliases: dict[str, dict[str, Any]] = {}
        self.ipsets: dict[str, dict[str, Any]] = {}  # name -> {"comment", "entries": [...]}


class FakeFirewall:
    def __init__(self, api: FakeAPI) -> None:
        self.api = api
        self.levels: dict[str, Level] = {}
        self.groups: dict[str, dict[str, Any]] = {}  # name -> {"comment", "rules": [...]}
        for method in ("GET", "POST", "PUT", "DELETE"):
            for pattern in (
                "/cluster/firewall/{a}", "/cluster/firewall/{a}/{b}", "/cluster/firewall/{a}/{b}/{c}",
                "/cluster/firewall/{a}/{b}/{c}/{d}",
                "/nodes/{n}/firewall/{a}", "/nodes/{n}/firewall/{a}/{b}",
                "/nodes/{n}/{k}/{v}/firewall/{a}", "/nodes/{n}/{k}/{v}/firewall/{a}/{b}",
                "/nodes/{n}/{k}/{v}/firewall/{a}/{b}/{c}", "/nodes/{n}/{k}/{v}/firewall/{a}/{b}/{c}/{d}",
                "/cluster/sdn/vnets/{v}/firewall/{a}", "/cluster/sdn/vnets/{v}/firewall/{a}/{b}",
            ):
                api.route(method, pattern, self.handle)

    # ── state helpers for the tests ──

    def level(self, base: str) -> Level:
        return self.levels.setdefault(base, Level())

    def rules(self, base: str) -> list[dict[str, Any]]:
        if base.startswith("/cluster/firewall/groups/"):
            return self.groups[base.rsplit("/", 1)[1]]["rules"]
        return self.level(base).rules

    # ── the API ──

    def handle(self, call: Call) -> Any:
        path = call.path
        base, _, rest = path.partition("/firewall")
        base += "/firewall"
        parts = [p for p in rest.split("/") if p]
        data = dict(call.data or {})
        params = dict(call.params or {})
        args = {**params, **data}
        if base == "/cluster/firewall" and parts and parts[0] == "groups":
            return self._groups(call.method, parts[1:], args)
        if base == "/cluster/firewall" and parts and parts[0] in ("macros", "refs"):
            return [] if parts[0] == "refs" else [{"macro": "SSH", "descr": "Secure shell"}]
        lv = self.level(base)
        what = parts[0]
        if what == "options":
            return self._options(call.method, lv, args)
        if what == "rules":
            return self._rules(call.method, lv.rules, parts[1:], args)
        if what == "aliases":
            return self._aliases(call.method, lv, parts[1:], args)
        if what == "ipset":
            return self._ipsets(call.method, lv, parts[1:], args)
        if what == "log":
            return []
        raise AssertionError(f"fake firewall: unknown {call.method} {path}")

    @staticmethod
    def _check(digest: str | None, current: Any) -> None:
        if digest and digest != _digest(current):
            raise APIError("detected modified configuration - file changed by other user? Try again.")

    @staticmethod
    def _listed(items: list[dict[str, Any]], digest_of: Any) -> list[dict[str, Any]]:
        d = _digest(digest_of)
        return [{**copy.deepcopy(i), "digest": d} for i in items]

    def _options(self, method: str, lv: Level, args: dict[str, Any]) -> Any:
        if method == "GET":
            return {**lv.options, "digest": _digest(lv.options)}
        self._check(args.pop("digest", None), lv.options)
        for key in [k.strip() for k in str(args.pop("delete", "")).split(",") if k.strip()]:
            lv.options.pop(key, None)
        lv.options.update(args)
        return None

    def _rules(self, method: str, rules: list[dict[str, Any]], parts: list[str], args: dict[str, Any]) -> Any:
        def listed() -> list[dict[str, Any]]:
            return self._listed([{**r, "pos": i} for i, r in enumerate(rules)], rules)

        if method == "GET":
            if parts:
                return listed()[int(parts[0])]
            return listed()
        self._check(args.pop("digest", None), rules)
        if method == "POST":
            args.pop("pos", None)  # ignored by Proxmox: a new rule goes first
            args.setdefault("enable", 1)
            rules.insert(0, dict(args))
            return None
        pos = int(parts[0])
        if pos >= len(rules):
            raise APIError(f"no rule at position {pos}")
        if method == "DELETE":
            rules.pop(pos)
            return None
        if "moveto" in args:
            to = int(args["moveto"])
            rule = rules[pos]
            rules.insert(to, rule)
            rules.pop(pos + 1 if to <= pos else pos)
            return None
        for key in [k.strip() for k in str(args.pop("delete", "")).split(",") if k.strip()]:
            rules[pos].pop(key, None)
        rules[pos].update(args)
        return None

    def _aliases(self, method: str, lv: Level, parts: list[str], args: dict[str, Any]) -> Any:
        items = lv.aliases
        if method == "GET":
            return self._listed([{"name": n, **a} for n, a in sorted(items.items())], items)
        if method == "POST":
            assert "digest" not in args, "POST aliases takes no digest"
            if args["name"] in items:
                raise APIError(f"alias '{args['name']}' already exists")
            items[args.pop("name")] = args
            return None
        self._check(args.pop("digest", None), items)
        name = parts[0]
        if method == "DELETE":
            items.pop(name)
            return None
        new = args.pop("rename", None)
        items[name] = dict(args)
        if new:
            items[new] = items.pop(name)
        return None

    def _ipsets(self, method: str, lv: Level, parts: list[str], args: dict[str, Any]) -> Any:
        sets = lv.ipsets
        if not parts:
            if method == "GET":
                return self._listed([{"name": n, **({"comment": s["comment"]} if s.get("comment") else {})}
                                     for n, s in sorted(sets.items())], {n: s.get("comment") for n, s in sets.items()})
            self._check(args.pop("digest", None), {n: s.get("comment") for n, s in sets.items()})
            name, old = args["name"], args.get("rename")
            if old:
                sets[name] = sets.pop(old)
                sets[name]["comment"] = args.get("comment", "")
            else:
                if name in sets:
                    raise APIError(f"IPSet '{name}' already exists")
                sets[name] = {"comment": args.get("comment", ""), "entries": []}
            return None
        name = parts[0]
        if name not in sets:
            raise APIError(f"no such IPSet '{name}'")
        entries = sets[name]["entries"]
        if len(parts) == 1:
            if method == "GET":
                return self._listed(entries, entries)
            if method == "POST":
                assert "digest" not in args, "POST ipset/{name} takes no digest"
                if any(e["cidr"] == args["cidr"] for e in entries):
                    raise APIError(f"address '{args['cidr']}' already exists")
                entries.append(args)
                return None
            if method == "DELETE":
                assert "digest" not in args, "DELETE ipset/{name} takes no digest"
                if entries and not args.get("force"):
                    raise APIError(f"IPSet '{name}' is not empty")
                sets.pop(name)
                return None
        cidr = "/".join(parts[1:])
        self._check(args.pop("digest", None), entries)
        if method == "DELETE":
            sets[name]["entries"] = [e for e in entries if e["cidr"] != cidr]
            return None
        if method == "PUT":
            entry = next(e for e in entries if e["cidr"] == cidr)
            entry.clear()
            entry.update({"cidr": cidr, **args})
            return None
        raise AssertionError(f"fake firewall: {method} ipset entry")

    def _groups(self, method: str, parts: list[str], args: dict[str, Any]) -> Any:
        groups = self.groups
        if not parts:
            if method == "GET":
                return self._listed([{"group": n, **({"comment": g["comment"]} if g.get("comment") else {})}
                                     for n, g in sorted(groups.items())], {n: g.get("comment") for n, g in groups.items()})
            self._check(args.pop("digest", None), {n: g.get("comment") for n, g in groups.items()})
            name, old = args["group"], args.get("rename")
            if old:
                groups[name] = groups.pop(old)
                # Proxmox carries the rules that insert the group along
                for lv in self.levels.values():
                    for r in lv.rules:
                        if r.get("type") == "group" and r.get("action") == old:
                            r["action"] = name
            else:
                groups[name] = {"comment": args.get("comment", ""), "rules": []}
            return None
        name = parts[0]
        if len(parts) == 1 and method == "DELETE":
            if groups[name]["rules"]:
                raise APIError(f"Security group '{name}' is not empty")
            groups.pop(name)
            return None
        return self._rules(method, groups[name]["rules"], parts[1:], args)
