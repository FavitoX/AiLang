#!/usr/bin/env python3
"""AILang JSON → S-expression converter.

One-off tool that translates legacy AILang JSON bytecode (v0.5/v0.6/v0.7) into
the S-expression format used from v0.8 onward. Known idioms (schemas, stores,
routes, handlers, instructions) emit their canonical S-expr shape; unknown
sections fall back to a generic `(section_name (key value) ...)` form so the
resulting `.sexpr` files are syntactically valid and round-trip through
`sexpr_parser.parse()`.

Usage:
    python json_to_sexpr.py <file.json> [> file.sexpr]
    python json_to_sexpr.py --inplace <file.json>   # writes file.sexpr next to file.json
"""

import json
import os
import re
import sys

INDENT = "  "
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-\.]*$")


def is_ident(s: str) -> bool:
    return bool(IDENT_RE.match(s))


def atom(v) -> str:
    """Emit a scalar as a single S-expr token."""
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s and is_ident(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def indent(text: str, n: int = 1) -> str:
    pad = INDENT * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


# ── Sections ────────────────────────────────────────────────────────────────

def emit_service(svc: dict, version: str) -> str:
    parts = [f'(service {svc["name"]}']
    parts.append(f'{INDENT}(version "{version}")')
    parts.append(f'{INDENT}(port {svc.get("port", 8080)})')
    if svc.get("on_fiber_crash"):
        parts.append(f'{INDENT}(on_fiber_crash {svc["on_fiber_crash"]})')
    if svc.get("on_service_crash"):
        parts.append(f'{INDENT}(on_service_crash {svc["on_service_crash"]})')
    parts[-1] += ")"
    return "\n".join(parts)


def emit_schema(name: str, sdef: dict) -> str:
    lines = [f"(schema {name}"]
    for field_name, f in sdef.get("fields", {}).items():
        constraints = []
        ftype = f.get("type", "any")
        list_of = f.get("of")
        tname = ftype if not list_of else f"(list {list_of})"
        if f.get("auto_gen"):
            constraints.append("(auto_gen)")
        if f.get("required"):
            constraints.append("(required)")
        for key in ("min", "max", "gt", "gte", "lt", "lte", "min_items"):
            if key in f:
                constraints.append(f"({key} {atom(f[key])})")
        if "default" in f:
            constraints.append(f"(default {atom(f['default'])})")
        if "values" in f:  # enum
            vals = " ".join(atom(v) for v in f["values"])
            constraints.append(f"(values {vals})")
        tail = " " + " ".join(constraints) if constraints else ""
        lines.append(f"{INDENT}(field {field_name} {tname}{tail})")
    lines[-1] += ")"
    return "\n".join(lines)


def emit_store(name: str, sdef: dict) -> str:
    lines = [f"(store {name}"]
    provider = sdef.get("provider") or sdef.get("type", "IN_MEMORY")
    lines.append(f"{INDENT}(provider {provider})")
    if "schema" in sdef:
        lines.append(f"{INDENT}(schema {sdef['schema']})")
    if "table" in sdef:
        lines.append(f'{INDENT}(table "{sdef["table"]}")')
    if "index" in sdef:
        idx = sdef["index"]
        lines.append(f'{INDENT}(index {idx.get("field","id")} {idx.get("type","PRIMARY")})')
    lines[-1] += ")"
    return "\n".join(lines)


def emit_auth(auth: dict) -> str:
    parts = ["(auth"]
    parts.append(f'{INDENT}(provider {auth.get("provider", "jwt")})')
    ref = auth.get("secret_ref", "jwt_secret")
    # Strip legacy `ENV.` prefix — v0.8 references the config key directly.
    if isinstance(ref, str) and ref.startswith("ENV."):
        ref = ref[4:].lower()
    parts.append(f"{INDENT}(secret_ref {ref})")
    parts[-1] += ")"
    return "\n".join(parts)


def emit_cors(cors: dict) -> str:
    parts = ["(cors"]
    origins = cors.get("origins", ["*"])
    parts.append(f"{INDENT}(origins " + " ".join(atom(o) for o in origins) + ")")
    parts.append(f"{INDENT}(methods " + " ".join(cors.get("methods", ["GET"])) + ")")
    parts.append(f"{INDENT}(headers " + " ".join(atom(h) for h in cors.get("headers", ["Content-Type"])) + ")")
    parts.append(f'{INDENT}(max_age {cors.get("max_age", 3600)})')
    parts[-1] += ")"
    return "\n".join(parts)


def emit_route(r: dict) -> str:
    return f'(route {r["method"]:6s} "{r["path"]}" {r["handler"]})'


# ── Instructions ────────────────────────────────────────────────────────────

def emit_instruction(inst: dict, depth: int = 0) -> str:
    """Emit a single instruction dict as S-expr. Depth controls child indent."""
    op = inst.get("op")
    child_pad = INDENT * depth
    inner = INDENT * (depth + 1)

    if op == "COMMENT":
        return f';; {inst.get("text", "")}'

    # Simple store ops
    if op == "STORE_LIST_ALL":
        if inst.get("paginate"):
            pg = inst["paginate"]
            return f'(STORE_LIST_ALL {inst["store"]} (paginate (page {pg.get("page", 1)}) (limit {pg.get("limit", 20)}) (max_limit {pg.get("max_limit", 100)})) → {inst["assign"]})'
        return f'(STORE_LIST_ALL {inst["store"]} → {inst["assign"]})'

    if op == "STORE_FIND":
        return f'(STORE_FIND {inst["store"]} {inst["key"]} → {inst["assign"]})'

    if op == "STORE_INSERT":
        return f'(STORE_INSERT {inst["store"]} {inst["data"]} → {inst["assign"]})'

    if op == "STORE_UPDATE":
        return f'(STORE_UPDATE {inst["store"]} {inst["key"]} {inst["data"]} → {inst["assign"]})'

    if op == "STORE_UPDATE_FIELDS":
        fields = inst.get("fields", {})
        field_forms = " ".join(f"({k} {atom(v)})" for k, v in fields.items())
        assign = f' → {inst["assign"]}' if inst.get("assign") else ""
        return f'(STORE_UPDATE_FIELDS {inst["store"]} {inst["key"]} ({field_forms}){assign})'

    if op == "STORE_DELETE":
        return f'(STORE_DELETE {inst["store"]} {inst["key"]})'

    # HTTP
    if op == "READ_PARAM":
        return f'(READ_PARAM {inst["name"]} {inst["type"]} → {inst["assign"]})'

    if op == "READ_BODY":
        return f'(READ_BODY {inst["schema"]} → {inst["assign"]})'

    if op == "RESPOND":
        body = f' {atom(inst["body"])}' if inst.get("body") is not None else ""
        return f'(RESPOND {inst["status"]}{body})'

    if op == "RESPOND_STATUS":
        return f'(RESPOND_STATUS {inst["status"]})'

    # Variables / expressions
    if op == "DEFINE_VAR":
        val = inst.get("value")
        name = inst["name"]
        type_info = f' {inst["type"]}' if inst.get("type") else ""
        if isinstance(val, dict):
            # Dict default → declare nil, then CONSTRUCT object to assign.
            declare = f'(DEFINE_VAR {name}{type_info} nil)'
            fields = "\n".join(f"{inner}({k} {atom(v)})" for k, v in val.items())
            construct = f'(CONSTRUCT object\n{fields}\n{child_pad}→ {name})'
            return declare + "\n" + child_pad + construct
        if isinstance(val, list) and not val:
            val_s = "(list)"
        elif val is None:
            val_s = "nil"
        else:
            val_s = atom(val)
        return f'(DEFINE_VAR {name}{type_info} {val_s})'

    if op == "SET_VAR":
        val = inst.get("value", "nil")
        name = inst["name"]
        if isinstance(val, dict):
            # Dict value → CONSTRUCT object inline.
            fields = "\n".join(f"{inner}({k} {atom(v)})" for k, v in val.items())
            return f'(CONSTRUCT object\n{fields}\n{child_pad}→ {name})'
        return f'(SET_VAR {name} {atom(val)})'

    if op == "COMPUTE":
        left_raw = inst.get("left")
        right_raw = inst.get("right")
        # Flatten nested sub-expressions into preceding temp COMPUTEs so the
        # result is always `(COMPUTE atom op atom → assign)`.
        prefix_lines = []
        if isinstance(left_raw, dict) and "op" in left_raw:
            tmp = f"_tmp_{inst['assign']}_l"
            prefix_lines.append(emit_instruction({
                "op": "COMPUTE", "left": left_raw.get("left"),
                "operator": left_raw["op"], "right": left_raw.get("right"),
                "assign": tmp,
            }, depth))
            left_raw = tmp
        if isinstance(right_raw, dict) and "op" in right_raw:
            tmp = f"_tmp_{inst['assign']}_r"
            prefix_lines.append(emit_instruction({
                "op": "COMPUTE", "left": right_raw.get("left"),
                "operator": right_raw["op"], "right": right_raw.get("right"),
                "assign": tmp,
            }, depth))
            right_raw = tmp
        line = (
            f'(COMPUTE {atom(left_raw)} {inst.get("operator", "+")} '
            f'{atom(right_raw)} → {inst["assign"]})'
        )
        if prefix_lines:
            return "\n".join(prefix_lines) + "\n" + child_pad + line
        return line

    if op == "COMPUTE_FN":
        args = " ".join(atom(a) for a in inst.get("args", []))
        return f'(COMPUTE_FN {inst.get("function","")} {args} → {inst["assign"]})'

    if op == "CONSTRUCT":
        data = inst.get("data", {})
        data_forms = []
        for k, v in data.items():
            if isinstance(v, dict):
                data_forms.append(emit_generic_form(k, v, depth + 2))
            else:
                data_forms.append(f"({k} {atom(v)})")
        body = "\n".join(inner + f for f in data_forms)
        return (
            f'(CONSTRUCT {inst["schema"]}\n'
            f"{body}\n"
            f'{child_pad}→ {inst["assign"]})'
        )

    if op == "GET_CURRENT":
        # Legacy JSON uses "what" (timestamp / day_of_week / auth.user) instead
        # of the canonical (from, field) pair. Normalize here.
        what = inst.get("what")
        from_ = inst.get("from")
        field = inst.get("field")
        if what and not from_:
            what_map = {
                "timestamp": ("clock", "now"),
                "day_of_week": ("clock", "day_of_week"),
                "today": ("clock", "today"),
            }
            if what.startswith("auth"):
                from_, field = "auth", what.split(".")[-1] if "." in what else "user"
            else:
                from_, field = what_map.get(what, ("clock", what))
        from_ = from_ or "auth"
        field = field or "user"
        return f'(GET_CURRENT {from_} {field} → {inst["assign"]})'

    if op == "APPEND":
        value = inst.get("value", {})
        if isinstance(value, dict):
            value_lines = "\n".join(inner + f"({k} {atom(v)})" for k, v in value.items())
            return (
                f'(APPEND {inst["target"]}\n'
                f"{value_lines})"
            )
        return f'(APPEND {inst["target"]} {atom(value)})'

    # Control flow
    if op == "MATCH":
        cases = inst.get("cases", {})
        case_parts = []
        for case_name, case_insts in cases.items():
            if not case_insts:
                case_parts.append(f"{inner}({case_name})")
                continue
            body = "\n".join(emit_instruction(i, depth + 2) for i in case_insts)
            body = indent(body, depth + 2)
            case_parts.append(f"{inner}({case_name}\n{body})")
        return f'(MATCH {inst["value"]}\n' + "\n".join(case_parts) + ")"

    if op == "BRANCH":
        cond = inst.get("condition", {})
        if isinstance(cond, dict):
            cond_s = f'({cond.get("op","==")} {atom(cond.get("left"))} {atom(cond.get("right"))})'
        else:
            cond_s = str(cond)
        cases = inst.get("cases", {})
        case_parts = []
        for case_name, case_insts in cases.items():
            if not case_insts:
                case_parts.append(f"{inner}({case_name})")
                continue
            body = "\n".join(emit_instruction(i, depth + 2) for i in case_insts)
            body = indent(body, depth + 2)
            case_parts.append(f"{inner}({case_name}\n{body})")
        return f"(BRANCH {cond_s}\n" + "\n".join(case_parts) + ")"

    if op == "ITERATE":
        body = "\n".join(emit_instruction(i, depth + 2) for i in inst.get("body", []))
        body = indent(body, depth + 1)
        return (
            f'(ITERATE {inst["collection"]} {inst.get("item", "item")}\n'
            f"{body})"
        )

    if op == "ITERATE_PARALLEL":
        body = "\n".join(emit_instruction(i, depth + 2) for i in inst.get("body", []))
        body = indent(body, depth + 1)
        return (
            f'(ITERATE_PARALLEL {inst["collection"]} {inst.get("item","item")}\n'
            f"{body})"
        )

    if op == "PARALLEL":
        # JSON shape: {op, assign, fibers: [{name, instructions: [...]}]}
        fibers = inst.get("fibers") or inst.get("branches") or []
        parts = []
        for b in fibers:
            name = b.get("name", "fiber")
            body = "\n".join(emit_instruction(i, depth + 2) for i in b.get("instructions", []))
            body = indent(body, depth + 2)
            parts.append(f"{inner}(fiber {name}\n{body})")
        assign = inst.get("assign")
        head = f"(PARALLEL → {assign}" if assign else "(PARALLEL"
        return f"{head}\n" + "\n".join(parts) + ")"

    if op == "AWAIT_ALL":
        # JSON shape: {op, source, timeout_ms}  OR  {op, items, assign}
        source = inst.get("source") or " ".join(inst.get("items", []))
        assign = inst.get("assign")
        extras = []
        if inst.get("timeout_ms") is not None:
            extras.append(f'(timeout_ms {inst["timeout_ms"]})')
        tail = " " + " ".join(extras) if extras else ""
        if assign:
            return f'(AWAIT_ALL {source}{tail} → {assign})'
        return f'(AWAIT_ALL {source}{tail})'

    if op == "IS_OK":
        return f'(IS_OK {inst["value"]} → {inst["assign"]})'

    if op == "TERNARY":
        return (
            f'(TERNARY {atom(inst.get("condition"))} '
            f'{atom(inst.get("then"))} {atom(inst.get("else"))} → {inst["assign"]})'
        )

    # Errors
    if op == "ERR_NOT_FOUND":
        return f'(ERR_NOT_FOUND "{inst["entity"]}" {inst["id"]})'
    if op == "ERR_INVALID_BODY":
        return f'(ERR_INVALID_BODY "{inst["schema"]}")'
    if op == "ERR_BUSINESS_RULE":
        return f'(ERR_BUSINESS_RULE "{inst["rule"]}" "{inst.get("detail","")}")'
    if op == "ERR_UNAVAILABLE":
        svc = inst.get("service", "")
        return f'(ERR_UNAVAILABLE "{svc}")'
    if op == "ERR_UNAUTHORIZED":
        return "(ERR_UNAUTHORIZED)"
    if op == "ERR_TIMEOUT":
        return "(ERR_TIMEOUT)"
    if op == "ERR_VALIDATION":
        return f'(ERR_VALIDATION "{inst.get("field","")}" "{inst.get("reason","")}")'
    if op == "ERR_INVALID_PARAM":
        return f'(ERR_INVALID_PARAM "{inst.get("name","")}" "{inst.get("expected","")}")'
    if op == "ERR_CUSTOM":
        params = inst.get("params", {})
        param_forms = " ".join(f"({k} {atom(v)})" for k, v in params.items())
        if param_forms:
            return f'(ERR_CUSTOM {inst["type"]} {param_forms})'
        return f'(ERR_CUSTOM {inst["type"]})'

    # Events / logging / service calls
    if op == "EMIT_EVENT":
        data = inst.get("data", "")
        return f'(EMIT_EVENT {inst["event"]} {data})'

    if op == "LOG":
        return f'(LOG {inst.get("level","INFO")} "{inst.get("message","")}")'

    if op == "CALL_SERVICE":
        body_field = ""
        if inst.get("body") is not None:
            b = inst["body"]
            if isinstance(b, dict):
                body_forms = " ".join(f"({k} {atom(v)})" for k, v in b.items())
                body_field = f"\n{inner}(body {body_forms})"
            else:
                body_field = f"\n{inner}(body {atom(b)})"
        idem = "true" if inst.get("idempotent") else "false"
        return (
            f'(CALL_SERVICE {inst["service"]}\n'
            f'{inner}(method {inst["method"]})\n'
            f'{inner}(path "{inst["path"]}")\n'
            f"{inner}(idempotent {idem})"
            f"{body_field}\n"
            f'{child_pad}→ {inst["assign"]})'
        )

    if op == "SAGA_COMPENSATE":
        return f'(SAGA_COMPENSATE {inst.get("saga","")})'

    # Comparison ops produced by some bytecodes (==, !=, <, >, NOT_IN, etc.) —
    # these appear when the original JSON put a condition as a top-level
    # instruction. Emit as a literal form.
    if op in ("==", "!=", "<", ">", "<=", ">=", "NOT_IN", "IN"):
        return f'({op} {atom(inst.get("left"))} {atom(inst.get("right"))})'

    # Fallback — emit every remaining field as (key value)
    body_forms = []
    for k, v in inst.items():
        if k == "op":
            continue
        body_forms.append(f"({k} {atom(v) if not isinstance(v, (dict, list)) else '...'})")
    return f'({op} ' + " ".join(body_forms) + ")"


def emit_generic_form(name: str, value, depth: int = 0) -> str:
    """Emit a generic (name value) form for a dict/list/scalar."""
    pad = INDENT * depth
    inner = INDENT * (depth + 1)
    if isinstance(value, dict):
        lines = [f"({name}"]
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                lines.append(inner + emit_generic_form(k, v, depth + 1))
            else:
                lines.append(f"{inner}({k} {atom(v)})")
        lines[-1] += ")"
        return "\n".join(lines)
    if isinstance(value, list):
        if all(not isinstance(x, (dict, list)) for x in value):
            return f"({name} " + " ".join(atom(x) for x in value) + ")"
        lines = [f"({name}"]
        for item in value:
            if isinstance(item, dict):
                lines.append(inner + emit_generic_form("item", item, depth + 1))
            else:
                lines.append(f"{inner}{atom(item)}")
        lines[-1] += ")"
        return "\n".join(lines)
    return f"({name} {atom(value)})"


# ── Handlers / Tests ────────────────────────────────────────────────────────

def emit_handler(name: str, h: dict) -> str:
    lines = [f"(handler {name}"]
    auth = h.get("auth")
    if auth == "PUBLIC":
        lines.append(f"{INDENT}(auth PUBLIC)")
    elif isinstance(auth, dict) and "requires" in auth:
        lines.append(f'{INDENT}(auth (requires {auth["requires"]}))')
    elif auth:
        lines.append(f"{INDENT}(auth {atom(auth)})")

    if h.get("type") == "EVENT_HANDLER":
        lines.append(f'{INDENT}(type EVENT_HANDLER)')
        if h.get("event"):
            lines.append(f'{INDENT}(event {h["event"]})')
    if h.get("params"):
        pforms = " ".join(f"({k} {v})" for k, v in h["params"].items())
        lines.append(f"{INDENT}(params {pforms})")
    if h.get("returns"):
        lines.append(f'{INDENT}(returns "{h["returns"]}")')

    instrs = h.get("instructions", [])
    if instrs:
        body = "\n".join(emit_instruction(i, 2) for i in instrs)
        body = indent(body, 2)
        lines.append(f"{INDENT}(do\n{body}))")
    else:
        lines[-1] += ")"
    return "\n".join(lines)


# ── Unknown sections (resilience / events / sagas / dependencies / custom_errors / tests) ──

def emit_generic_section(name: str, value) -> str:
    """Emit any top-level section as a generic nested S-expr tree."""
    return emit_generic_form(name, value, 0)


# ── Top level ───────────────────────────────────────────────────────────────

def convert(json_data: dict, source_name: str = "") -> str:
    version = json_data.get("version", "0.8")
    chunks = [
        ";; ═══════════════════════════════════════════════════════════════",
        f";; AILang Bytecode — S-expression format",
        f";; Service: {json_data.get('service', {}).get('name', 'unknown')}",
        f";; Version: {version}",
        f";; Converted from: {source_name}" if source_name else ";;",
        ";; ═══════════════════════════════════════════════════════════════",
        "",
    ]

    if "service" in json_data:
        chunks.append(emit_service(json_data["service"], version))
        chunks.append("")

    if "schemas" in json_data:
        chunks.append(";; ── Schemas ──")
        for name, sdef in json_data["schemas"].items():
            chunks.append(emit_schema(name, sdef))
        chunks.append("")

    if "stores" in json_data:
        chunks.append(";; ── Stores ──")
        for name, sdef in json_data["stores"].items():
            chunks.append(emit_store(name, sdef))
        chunks.append("")

    if "dependencies" in json_data:
        chunks.append(";; ── Dependencies ──")
        chunks.append(emit_generic_section("dependencies", json_data["dependencies"]))
        chunks.append("")

    if "auth" in json_data:
        chunks.append(";; ── Auth ──")
        chunks.append(emit_auth(json_data["auth"]))
        chunks.append("")

    if "cors" in json_data:
        chunks.append(";; ── CORS ──")
        chunks.append(emit_cors(json_data["cors"]))
        chunks.append("")

    if "resilience" in json_data:
        chunks.append(";; ── Resilience (policies, events) ──")
        chunks.append(emit_generic_section("resilience", json_data["resilience"]))
        chunks.append("")

    if "custom_errors" in json_data:
        chunks.append(";; ── Custom Errors ──")
        chunks.append(emit_generic_section("custom_errors", json_data["custom_errors"]))
        chunks.append("")

    if "events" in json_data:
        chunks.append(";; ── Events (definitions + subscriptions) ──")
        chunks.append(emit_generic_section("events", json_data["events"]))
        chunks.append("")

    if "sagas" in json_data:
        chunks.append(";; ── Sagas ──")
        chunks.append(emit_generic_section("sagas", json_data["sagas"]))
        chunks.append("")

    if "discount_rules" in json_data:
        chunks.append(";; ── Discount Rules (pricing-specific) ──")
        chunks.append(emit_generic_section("discount_rules", json_data["discount_rules"]))
        chunks.append("")

    if "expressions" in json_data:
        chunks.append(";; ── Expression Mini-Language Spec ──")
        chunks.append(emit_generic_section("expressions", json_data["expressions"]))
        chunks.append("")

    if "routes" in json_data:
        chunks.append(";; ── Routes ──")
        for r in json_data["routes"]:
            chunks.append(emit_route(r))
        chunks.append("")

    if "handlers" in json_data:
        chunks.append(";; ── Handlers ──")
        for name, h in json_data["handlers"].items():
            chunks.append(emit_handler(name, h))
            chunks.append("")

    if "tests" in json_data:
        chunks.append(";; ── Tests (legacy format — preserved as nested tree) ──")
        chunks.append(emit_generic_section("tests", json_data["tests"]))
        chunks.append("")

    return "\n".join(chunks)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__.strip())
        sys.exit(1)
    inplace = False
    if argv[0] == "--inplace":
        inplace = True
        argv = argv[1:]
    if not argv:
        print("usage: json_to_sexpr.py [--inplace] <file.json>", file=sys.stderr)
        sys.exit(1)
    source = argv[0]
    with open(source, "r") as f:
        data = json.load(f)
    out = convert(data, source_name=os.path.basename(source))
    if inplace:
        target = os.path.splitext(source)[0] + ".sexpr"
        with open(target, "w") as f:
            f.write(out)
        print(f"wrote {target}")
    else:
        print(out)


if __name__ == "__main__":
    main()
