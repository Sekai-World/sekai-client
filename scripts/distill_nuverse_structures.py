"""Distill nuverse positional schemas from the Haruki bundle + live data.

Run on the production host (needs the venv, the account RPC and the upstream
blob). Emits a Python data module plus a verification report.

Usage: distill_nuverse_structures.py OUT_MODULE.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/root/sekai-client-main")
os.chdir("/root/sekai-client-main")

from utils.array_to_dict import convert_array_to_dict  # noqa: E402
from utils.constants import nuverse_master_data_base_url  # noqa: E402
from utils.crypto import decrypt_msgpack  # noqa: E402
from utils.jsonrpc_client import JSONRPCClient  # noqa: E402

BUNDLE_PATH = "/tmp/nuverse_schema_bundle.json"
PRIMS = {"string", "int", "long", "float", "boolean", "bytes"}
MAX_DEPTH = 6

# Cards table gained a third column upstream that the bundle does not carry
# yet; verified against the live blob and the EN master DB field set.
EXTRA_TRAILING_FIELDS = {"cardCostume3ds": ["isInitialObtainHair"]}


def load_blob() -> tuple[dict, int]:
    """Fetch and decrypt the current live master data blob."""
    client = JSONRPCClient("http://localhost:" + os.environ["JSONRPC_PORT"] + "/")
    cdn = client.request("version_info")["cdnVersion"]
    import requests

    res = requests.get(
        nuverse_master_data_base_url["tw"] + f"/master-data-{cdn}.info",
        timeout=60,
    )
    return decrypt_msgpack(res.content), cdn


def classify(ftype):
    """Classify an avro-ish type -> (kind, payload)."""
    if isinstance(ftype, list):
        subs = [t for t in ftype if t != "null"]
        if not subs:
            return "plain", None
        if all(isinstance(t, str) and t in PRIMS for t in subs):
            return "plain", None
        if len(set(map(str, subs))) == 1:
            return classify(subs[0])
        return "plain", None
    if isinstance(ftype, dict):
        if ftype.get("type") == "array":
            return "array", ftype.get("items")
        return "plain", None
    if ftype in PRIMS:
        return "plain", None
    return "named", ftype


def build_observed_kinds(blob: dict) -> dict:
    """Sampled per-column value type names for every positional table."""
    observed = {}
    for table, records in blob.items():
        if not isinstance(records, list) or not records:
            continue
        if not isinstance(records[0], list):
            continue
        kinds = []
        for i in range(len(records[0])):
            vals = [r[i] for r in records[:50] if len(r) > i]
            kinds.append(sorted({type(v).__name__ for v in vals}))
        observed[table] = kinds
    return observed


def build_specs(bundle: dict, schemas: dict, observed: dict) -> dict:  # noqa: C901
    """Build positional specs for every bundle table that has contiguous keys.

    Complexity note: the nested ``build_spec`` walks the recursive type tree
    (plain columns, arrays of structs, flat-tuple structs) and the branches
    mirror the spec-language productions one to one; splitting it further
    would hide that mapping.
    """

    def ref_schema(name):
        return schemas.get((name or "").split("/")[-1].split(".")[-1])

    def spec_for_named_scalar(table, name, payload, i, depth):
        """Spec for a named-struct column delivered as a scalar value.

        The upstream source is inconsistent here: some named structs arrive
        decoded (dict, pass through as-is) and others as one positional array
        (decodable via a flat tuple mapping when its own fields are scalars).
        """
        sub = ref_schema(payload)
        seen = observed.get(table, [])
        col_kinds = seen[i] if i < len(seen) else []
        if "dict" in col_kinds or sub is None or "list" not in col_kinds:
            return name
        sub_fields = sorted(sub["fields"], key=lambda f: f["msgpack_key"])
        if all(classify(f["type"])[0] == "plain" for f in sub_fields):
            return [name, tuple(f["name"] for f in sub_fields)]
        return name

    def build_spec(table, sch, depth):
        if depth > MAX_DEPTH:
            raise ValueError("depth limit")
        fields = sorted(sch["fields"], key=lambda f: f["msgpack_key"])
        keys = [f["msgpack_key"] for f in fields]
        if keys != list(range(len(fields))):
            raise ValueError("non-contiguous msgpack_key")
        spec = []
        for i, f in enumerate(fields):
            name, ftype = f["name"], f["type"]
            kind, payload = classify(ftype)
            if kind == "plain":
                spec.append(name)
            elif kind == "array":
                items = payload
                if items is None or (isinstance(items, str) and items in PRIMS):
                    spec.append(name)
                    continue
                sub = ref_schema(items) if isinstance(items, str) else None
                if sub is None:
                    spec.append(name)
                    continue
                spec.append([name, build_spec(table, sub, depth + 1)])
            else:
                spec.append(spec_for_named_scalar(table, name, payload, i, depth + 1))
        spec.extend(EXTRA_TRAILING_FIELDS.get(table, []))
        return spec

    specs = {}
    for table in sorted(bundle["master"]):
        sch = schemas.get(bundle["master"][table].split(".")[-1])
        if sch is None:
            continue
        try:
            specs[table] = build_spec(table, sch, 0)
        except ValueError:
            continue
    return specs


def verify_specs(blob: dict, specs: dict) -> tuple[int, int, list, list]:
    """Convert sample records per positional table; report failures."""
    list_tables = [
        t
        for t, r in blob.items()
        if isinstance(r, list) and r and isinstance(r[0], list)
    ]
    failures = []
    ok = 0
    for table in list_tables:
        spec = specs.get(table)
        if spec is None:
            continue
        try:
            for r in blob[table][:200]:
                convert_array_to_dict(r, spec, structure_name=table)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failures.append((table, type(exc).__name__, str(exc)[:150]))
    missing = [t for t in list_tables if t not in specs]
    return len(list_tables), ok, missing, failures


def main() -> None:
    blob, cdn = load_blob()
    with open(BUNDLE_PATH) as f:
        bundle = json.load(f)
    schemas = {s["name"]: s for s in bundle["schemas"]}

    observed = build_observed_kinds(blob)
    specs = build_specs(bundle, schemas, observed)
    checked, ok, missing, failures = verify_specs(blob, specs)

    module = emit_module(specs, cdn)
    with open(sys.argv[1], "w") as f:
        f.write(module)

    tuple_specs = sum(
        1
        for spec in specs.values()
        for key in spec
        if isinstance(key, list) and isinstance(key[1], tuple)
    )
    print("cdn:", cdn)
    print("list-form tables:", checked)
    print("specs built:", len(specs))
    print("verified conversions:", ok)
    print("missing specs:", missing)
    print("conversion failures:", failures)
    print("flat-tuple specs:", tuple_specs)
    print("module bytes:", len(module))


def emit_module(specs: dict, cdn) -> str:
    """Render the specs dict as the generated data module text."""
    lines = [
        '"""Generated positional schemas for nuverse (cn/tw/kr) master data.',
        "",
        "Field orders for tables the upstream source delivers as positional",
        "arrays. Distilled from Team-Haruki/Haruki-Sekai-API",
        f"Data/structures/nuverse_schema_bundle.json (master data cdnVersion {cdn})",
        "and verified against the live blob by",
        "scripts/distill_nuverse_structures.py. Regenerate rather than edit;",
        "the one intentional divergence (cardCostume3ds isInitialObtainHair)",
        "is recorded in EXTRA_TRAILING_FIELDS inside the distiller.",
        '"""',
        "",
        "# fmt: off",
        "NUVERSE_POSITIONAL_STRUCTURES = (",
    ]
    lines.append(emitter(specs, 1))
    lines.append(")")
    return "\n".join(lines) + "\n"


def emitter(node, indent: int) -> str:
    """Render a spec node as a formatted Python literal (tuples preserved)."""
    pad = "    " * indent
    if isinstance(node, tuple):
        return "(" + ", ".join(repr(k) for k in node) + ",)"
    if isinstance(node, list):
        if (
            len(node) == 2
            and isinstance(node[0], str)
            and isinstance(node[1], (tuple, list))
        ):
            head = emitter(node[0], indent)
            return "[" + head + ", " + emitter(node[1], indent) + "]"
        if not node:
            return "[]"
        inner = ",\n".join(pad + "    " + emitter(v, indent + 1) for v in node)
        return "[\n" + inner + ",\n" + pad + "]"
    if isinstance(node, dict):
        if not node:
            return "{}"
        inner = ",\n".join(
            pad + "    " + repr(k) + ": " + emitter(v, indent + 1)
            for k, v in node.items()
        )
        return "{\n" + inner + ",\n" + pad + "}"
    return repr(node)


if __name__ == "__main__":
    main()
