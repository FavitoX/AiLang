#!/usr/bin/env python3
"""
AILang Transpiler v0.1
Reads AILang bytecode (JSON) and generates functional C# and Go code.

Usage:
    python transpiler.py <bytecode.json> --target csharp
    python transpiler.py <bytecode.json> --target go
    python transpiler.py <bytecode.json> --target all
"""

import json
import sys
import os
from abc import ABC, abstractmethod
from typing import Any


# ============================================================================
# BYTECODE PARSER
# ============================================================================

class BytecodeParser:
    """Parses and validates AILang bytecode JSON."""

    def __init__(self, filepath: str):
        with open(filepath, 'r') as f:
            self.raw = json.load(f)
        self.validate()

    def validate(self):
        required = ['service', 'schemas', 'routes', 'handlers']
        for key in required:
            if key not in self.raw:
                raise ValueError(f"Missing required key: {key}")

        # Validate all handlers have auth
        for name, handler in self.raw['handlers'].items():
            if 'auth' not in handler:
                raise ValueError(f"Handler '{name}' missing auth (PUBLIC or REQUIRES). AILang requires auth on every handler.")

        # Validate routes reference existing handlers
        for route in self.raw['routes']:
            if route['handler'] not in self.raw['handlers']:
                raise ValueError(f"Route references unknown handler: {route['handler']}")

    @property
    def service(self): return self.raw['service']

    @property
    def schemas(self): return self.raw.get('schemas', {})

    @property
    def stores(self): return self.raw.get('stores', {})

    @property
    def cors(self): return self.raw.get('cors', {})

    @property
    def auth(self): return self.raw.get('auth', {})

    @property
    def config(self): return self.raw.get('config', {})

    @property
    def routes(self): return self.raw.get('routes', [])

    @property
    def handlers(self): return self.raw.get('handlers', {})

    @property
    def custom_errors(self): return self.raw.get('custom_errors', {})

    @property
    def tests(self): return self.raw.get('tests', [])

    @property
    def dependencies(self): return self.raw.get('dependencies', {})

    @property
    def resilience(self): return self.raw.get('resilience', {})


# ============================================================================
# TYPE MAPPER
# ============================================================================

class TypeMapper:
    """Maps AILang types to target language types."""

    CSHARP_TYPES = {
        'uuid': 'Guid',
        'string': 'string',
        'int': 'int',
        'decimal': 'decimal',
        'bool': 'bool',
        'timestamp': 'DateTime',
        'list': 'List',
        'enum': 'string',  # Simplified for MVP
        'object': 'object',
    }

    GO_TYPES = {
        'uuid': 'string',
        'string': 'string',
        'int': 'int',
        'decimal': 'float64',
        'bool': 'bool',
        'timestamp': 'time.Time',
        'list': '[]',
        'enum': 'string',
        'object': 'map[string]any',
    }

    @staticmethod
    def to_csharp(ailang_type: str, list_of: str = None) -> str:
        if ailang_type == 'list':
            if list_of:
                inner = TypeMapper.CSHARP_TYPES.get(list_of, list_of)
                return f"List<{inner}>"
            return "List<object>"
        return TypeMapper.CSHARP_TYPES.get(ailang_type, ailang_type)

    @staticmethod
    def to_go(ailang_type: str, list_of: str = None) -> str:
        if ailang_type == 'list' and list_of:
            inner = TypeMapper.GO_TYPES.get(list_of, list_of)
            return f"[]{inner}"
        return TypeMapper.GO_TYPES.get(ailang_type, ailang_type)


# ============================================================================
# SQL TYPE HELPERS (PostgreSQL)
# ============================================================================

def sql_type_to_csharp(sql_type: str) -> str:
    """Map a SQL type (e.g. VARCHAR(50), NUMERIC(10,2), UUID) to the C# type."""
    if not sql_type:
        return 'object'
    t = sql_type.upper().strip()
    if t == 'UUID':
        return 'Guid'
    if t.startswith('VARCHAR') or t.startswith('CHAR') or t == 'TEXT':
        return 'string'
    if t.startswith('NUMERIC') or t.startswith('DECIMAL'):
        return 'decimal'
    if t in ('INT', 'INTEGER', 'INT4', 'SERIAL'):
        return 'int'
    if t in ('BIGINT', 'INT8', 'BIGSERIAL'):
        return 'long'
    if t in ('SMALLINT', 'INT2'):
        return 'short'
    if t in ('BOOLEAN', 'BOOL'):
        return 'bool'
    if t.startswith('TIMESTAMP') or t == 'DATE' or t == 'TIME':
        return 'DateTime'
    if t.startswith('JSON'):
        return 'string'
    return 'object'


def sql_type_to_reader_getter(sql_type: str, index: int) -> str:
    """Map a SQL type to an NpgsqlDataReader getter call."""
    if not sql_type:
        return f'reader.GetValue({index})'
    t = sql_type.upper().strip()
    if t == 'UUID':
        return f'reader.GetGuid({index})'
    if t.startswith('VARCHAR') or t.startswith('CHAR') or t == 'TEXT':
        return f'reader.GetString({index})'
    if t.startswith('NUMERIC') or t.startswith('DECIMAL'):
        return f'reader.GetDecimal({index})'
    if t in ('INT', 'INTEGER', 'INT4', 'SERIAL'):
        return f'reader.GetInt32({index})'
    if t in ('BIGINT', 'INT8', 'BIGSERIAL'):
        return f'reader.GetInt64({index})'
    if t in ('SMALLINT', 'INT2'):
        return f'reader.GetInt16({index})'
    if t in ('BOOLEAN', 'BOOL'):
        return f'reader.GetBoolean({index})'
    if t.startswith('TIMESTAMP') or t == 'DATE' or t == 'TIME':
        return f'reader.GetDateTime({index})'
    return f'reader.GetValue({index})'


def where_to_sql(expr) -> str:
    """Translate an AILang (where ...) S-expression subtree to a SQL fragment."""
    if expr is None:
        return 'TRUE'
    if isinstance(expr, bool):
        return 'TRUE' if expr else 'FALSE'
    if isinstance(expr, (int, float)):
        return str(expr)
    if isinstance(expr, str):
        if expr == 'true':
            return 'TRUE'
        if expr == 'false':
            return 'FALSE'
        return expr
    if not isinstance(expr, list) or not expr:
        return 'TRUE'

    op = expr[0]
    if op == 'and':
        return ' AND '.join(f'({where_to_sql(a)})' for a in expr[1:])
    if op == 'or':
        return ' OR '.join(f'({where_to_sql(a)})' for a in expr[1:])
    if op == 'not' and len(expr) > 1:
        return f'NOT ({where_to_sql(expr[1])})'
    if op in ('=', '!=', '<', '>', '<=', '>='):
        sql_op = '<>' if op == '!=' else op
        left = where_to_sql(expr[1]) if len(expr) > 1 else ''
        right = where_to_sql(expr[2]) if len(expr) > 2 else ''
        return f'{left} {sql_op} {right}'
    return str(expr)


def generate_postgres_ddl(store_def: dict) -> str:
    """Build a CREATE TABLE IF NOT EXISTS statement from a PG store mapping.

    Column definition per entry: `"col" TYPE [NOT NULL] [UNIQUE] [DEFAULT expr]`.
    The `id` field (by convention) becomes the PRIMARY KEY — matches how
    FindAsync/UpdateAsync/DeleteAsync key off `id` in the generated store.
    """
    table = store_def.get('table') or 'items'
    mapping = list(store_def.get('mapping', {}).items())
    if not mapping:
        return f"-- WARN: no mapping for table {table}"
    cols = []
    pk_field = None
    for field, m in mapping:
        parts = [f'"{m["column"]}" {m["sql_type"]}']
        if m.get('not_null'):
            parts.append("NOT NULL")
        if m.get('unique'):
            parts.append("UNIQUE")
        if m.get('default') is not None:
            parts.append(f"DEFAULT {m['default']}")
        cols.append("  " + " ".join(parts))
        if field == 'id':
            pk_field = m['column']
    if pk_field:
        cols.append(f'  PRIMARY KEY ("{pk_field}")')
    return f"CREATE TABLE IF NOT EXISTS \"{table}\" (\n" + ",\n".join(cols) + "\n);"


def emit_sql_literal_csharp(sql: str, indent_level: int = 1) -> str:
    """Emit a SQL literal as a C# raw string (\"\"\"...\"\"\") for multi-line SQL,
    or a plain string literal for short single-line SQL.

    `indent_level` is the nesting level of the surrounding code (in 4-space units).
    """
    # Strip trailing whitespace; skip blank lines.
    raw_lines = [line.rstrip() for line in sql.splitlines()]
    non_blank = [line for line in raw_lines if line.strip()]

    if len(non_blank) <= 1:
        one = ' '.join(l.strip() for l in non_blank)
        escaped = one.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'

    # Multi-line → raw string literal. The closing `"""` column determines the
    # leading whitespace stripped from each content line. We pick an indent of
    # `indent_level + 1` 4-space stops.
    content_pad = "    " * (indent_level + 1)
    body = '\n'.join(content_pad + line.strip() for line in non_blank)
    closing_pad = "    " * (indent_level + 1)
    return f'"""\n{body}\n{closing_pad}"""'


def ailang_param_to_csharp_value(param_type: str, var_name: str) -> str:
    """Return a C# expression converting a handler variable to the correct parameter type."""
    t = (param_type or 'string').lower()
    if t == 'uuid':
        return var_name  # already a Guid from READ_PARAM
    if t == 'int':
        return var_name
    if t == 'bool':
        return var_name
    if t == 'decimal':
        return var_name
    return var_name  # string / default


def path_to_csharp_interpolated(path: str) -> str:
    """Convert '/products/{item.product_id}' to a C# interpolated string literal
    `$"/products/{item.ProductId}"` — member access segments are PascalCased to
    match C# property conventions."""
    import re
    def repl(m):
        expr = m.group(1)
        if '.' in expr:
            parts = expr.split('.')
            expr = parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])
        return '{' + expr + '}'
    s = re.sub(r'\{([a-zA-Z_][\w.]*)\}', repl, path or '')
    return f'$"{s}"'


def path_to_go_sprintf(path: str, ctx: dict = None):
    """Convert '/products/{item.product_id}' to (fmt_string_with_%v, [arg_exprs]).

    Each {x.y.z} placeholder becomes %v; the corresponding arg is `x.Y.Z` (Go
    idioms: first segment lowercase, subsequent PascalCased). When ctx is
    provided and the path root is a JsonNode-like local (map/any_json), resolve
    via indexer access (x["y"]["z"]) instead of typed struct fields."""
    import re
    args = []
    locals_map = (ctx or {}).get('locals', {}) or {}
    def repl(m):
        expr = m.group(1)
        if '.' in expr:
            root = expr.split('.')[0]
            if locals_map.get(root) in ('map', 'any_json', 'JsonNode'):
                expr = _go_resolve_path(expr, ctx)
            else:
                parts = expr.split('.')
                expr = parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])
        args.append(expr)
        return '%v'
    fmt = re.sub(r'\{([a-zA-Z_][\w.]*)\}', repl, path or '')
    return fmt, args


def err_name_to_factory(err_type: str) -> str:
    """ERR_INSUFFICIENT_STOCK → InsufficientStock (C# factory name or Go suffix)."""
    name = str(err_type)
    if name.startswith('ERR_'):
        name = name[4:]
    return ''.join(w.capitalize() for w in name.split('_') if w)


def err_type_tag(err_type: str) -> str:
    """ERR_INSUFFICIENT_STOCK → INSUFFICIENT_STOCK (the `Type` field value)."""
    name = str(err_type)
    return name[4:] if name.startswith('ERR_') else name


def template_to_csharp_interpolated(template: str, param_camel: dict[str, str]) -> str:
    """Convert a `{snake_case}` placeholder template to a C# interpolated-string body.

    Only placeholders whose snake_case key is in `param_camel` are remapped;
    unknown placeholders are left as-is so misuse is visible in the output.
    """
    import re
    def repl(m):
        key = m.group(1)
        return '{' + param_camel.get(key, key) + '}'
    msg = re.sub(r'\{(\w+)\}', repl, template)
    return msg.replace('"', '\\"')


def template_to_go_sprintf(template: str, param_camel: dict[str, str]):
    """Convert a `{snake_case}` template to (fmt_string_with_%v, [arg_names_in_order])."""
    import re
    args = []
    def repl(m):
        key = m.group(1)
        args.append(param_camel.get(key, key))
        return '%v'
    fmt = re.sub(r'\{(\w+)\}', repl, template)
    return fmt.replace('"', '\\"'), args


def ailang_type_to_csharp(t) -> str:
    """Map an AILang type string (e.g. 'List<OrderItem>', 'decimal') to the C# type."""
    if t is None:
        return 'object'
    t = str(t)
    if t.startswith('List<') and t.endswith('>'):
        inner = t[5:-1]
        return f'List<{ailang_type_to_csharp(inner)}>'
    m = {
        'int': 'int', 'decimal': 'decimal', 'string': 'string', 'bool': 'bool',
        'object': 'object', 'uuid': 'Guid', 'timestamp': 'DateTime',
        'list': 'List<object>',
    }
    return m.get(t, t)


def ailang_type_to_go(t) -> str:
    if t is None:
        return 'any'
    t = str(t)
    if t.startswith('List<') and t.endswith('>'):
        inner = t[5:-1]
        return f'[]{ailang_type_to_go(inner)}'
    m = {
        'int': 'int', 'decimal': 'float64', 'string': 'string', 'bool': 'bool',
        'object': 'any', 'uuid': 'string', 'timestamp': 'time.Time',
        # Bare `list` (no element type) from DEFINE_VAR → heterogeneous slice.
        'list': '[]any',
    }
    return m.get(t, t)


def extract_list_elem(ailang_or_cs_type: str) -> str:
    """Given 'List<X>' return 'X'; else return 'object'."""
    t = str(ailang_or_cs_type or '')
    if t.startswith('List<') and t.endswith('>'):
        return t[5:-1]
    if t.startswith('[]'):
        return t[2:]
    return 'object'


def sql_type_to_go(sql_type: str) -> str:
    """Map a SQL type to the Go type used in struct fields / Scan destinations."""
    if not sql_type:
        return 'any'
    t = sql_type.upper().strip()
    if t == 'UUID':
        return 'string'
    if t.startswith('VARCHAR') or t.startswith('CHAR') or t == 'TEXT':
        return 'string'
    if t.startswith('NUMERIC') or t.startswith('DECIMAL'):
        return 'float64'
    if t in ('INT', 'INTEGER', 'INT4', 'SERIAL'):
        return 'int'
    if t in ('BIGINT', 'INT8', 'BIGSERIAL'):
        return 'int64'
    if t in ('SMALLINT', 'INT2'):
        return 'int16'
    if t in ('BOOLEAN', 'BOOL'):
        return 'bool'
    if t.startswith('TIMESTAMP') or t == 'DATE' or t == 'TIME':
        return 'time.Time'
    return 'any'


# ============================================================================
# SCHEMA GENERATOR
# ============================================================================

def generate_validation_csharp(field_name: str, field_def: dict, var_name: str) -> list[str]:
    """Generate C# validation code from schema constraints."""
    lines = []
    ftype = field_def.get('type')
    required = field_def.get('required', False)
    prop_name = pascal_case(field_name)

    if ftype == 'string' and required:
        min_len = field_def.get('min', 0)
        max_len = field_def.get('max', 0)
        if min_len > 0:
            lines.append(f'        if (string.IsNullOrWhiteSpace({var_name}.{prop_name}) || {var_name}.{prop_name}.Length < {min_len})')
            lines.append(f'            return Results.BadRequest(AiLangError.Validation("{field_name}", "required, min length {min_len}"));')
        if max_len > 0:
            lines.append(f'        if ({var_name}.{prop_name}?.Length > {max_len})')
            lines.append(f'            return Results.BadRequest(AiLangError.Validation("{field_name}", "max length {max_len}"));')

    if ftype in ('int', 'decimal'):
        gt = field_def.get('gt')
        gte = field_def.get('gte')
        if gt is not None:
            lines.append(f'        if ({var_name}.{prop_name} <= {gt}{"m" if ftype == "decimal" else ""})')
            lines.append(f'            return Results.BadRequest(AiLangError.Validation("{field_name}", "must be > {gt}"));')
        if gte is not None:
            lines.append(f'        if ({var_name}.{prop_name} < {gte})')
            lines.append(f'            return Results.BadRequest(AiLangError.Validation("{field_name}", "must be >= {gte}"));')

    if ftype == 'list':
        min_items = field_def.get('min_items')
        if min_items and required:
            lines.append(f'        if ({var_name}.{prop_name} == null || {var_name}.{prop_name}.Count < {min_items})')
            lines.append(f'            return Results.BadRequest(AiLangError.Validation("{field_name}", "required, min {min_items} items"));')

    return lines


def generate_validation_go(field_name: str, field_def: dict, var_name: str) -> list[str]:
    """Generate Go validation code from schema constraints."""
    lines = []
    ftype = field_def.get('type')
    required = field_def.get('required', False)
    prop_name = pascal_case(field_name)

    if ftype == 'string' and required:
        min_len = field_def.get('min', 0)
        max_len = field_def.get('max', 0)
        if min_len > 0:
            lines.append(f'\tif len(strings.TrimSpace({var_name}.{prop_name})) < {min_len} {{')
            lines.append(f'\t\treturn ErrValidation("{field_name}", "required, min length {min_len}")')
            lines.append('\t}')
        if max_len > 0:
            lines.append(f'\tif len({var_name}.{prop_name}) > {max_len} {{')
            lines.append(f'\t\treturn ErrValidation("{field_name}", "max length {max_len}")')
            lines.append('\t}')

    if ftype in ('int', 'decimal'):
        gt = field_def.get('gt')
        gte = field_def.get('gte')
        if gt is not None:
            lines.append(f'\tif {var_name}.{prop_name} <= {gt} {{')
            lines.append(f'\t\treturn ErrValidation("{field_name}", "must be > {gt}")')
            lines.append('\t}')
        if gte is not None:
            lines.append(f'\tif {var_name}.{prop_name} < {gte} {{')
            lines.append(f'\t\treturn ErrValidation("{field_name}", "must be >= {gte}")')
            lines.append('\t}')

    if ftype == 'list' and required:
        min_items = field_def.get('min_items')
        if min_items:
            lines.append(f'\tif len({var_name}.{prop_name}) < {min_items} {{')
            lines.append(f'\t\treturn ErrValidation("{field_name}", "required, min {min_items} items")')
            lines.append('\t}')

    return lines


# ============================================================================
# INSTRUCTION COMPILER — C#
# ============================================================================

def compile_instruction_csharp(inst: dict, indent: int = 2, ctx: dict = None) -> list[str]:
    """Compile a single AILang instruction to C# code.

    ctx carries per-handler codegen state:
      - 'store_var': C# variable name for the store (e.g. 'productStore')
      - 'store_def': the store definition (provider, mapping, table, schema)
      - 'is_pg':   True if the store uses PostgreSQL
      - 'returns': handler returns metadata
    """
    pad = "    " * indent
    op = inst.get('op')
    lines = []
    ctx = ctx or {}
    is_pg = ctx.get('is_pg', False)
    store_var = ctx.get('store_var', 'store')

    if op == 'COMMENT':
        lines.append(f"{pad}// {inst['text']}")

    elif op == 'STORE_LIST_ALL':
        assign = inst['assign']
        paginate = inst.get('paginate')
        if is_pg and paginate:
            page = paginate.get('page') or 1
            limit = paginate.get('limit') or 20
            max_limit = paginate.get('max_limit') or 100
            lines.append(f"{pad}var {assign} = await {store_var}.ListAllAsync({page}, {limit}, {max_limit});")
        elif is_pg:
            lines.append(f"{pad}var {assign} = await {store_var}.ListAllAsync(1, 100, 100);")
        else:
            lines.append(f"{pad}var {assign} = {store_var}.ListAll();")

    elif op == 'STORE_FIND':
        key = _cs_expr(inst['key'], ctx)
        if is_pg:
            lines.append(f"{pad}var {inst['assign']} = await {store_var}.FindAsync({key});")
        else:
            lines.append(f"{pad}var {inst['assign']} = {store_var}.Find({key});")
        # Register so downstream STORE_UPDATE_FIELDS doesn't re-declare the local
        # and so field-access codegen can resolve typed properties.
        store_schema = (ctx.get('store_def') or {}).get('schema', '')
        if store_schema:
            ctx.setdefault('locals', {})[inst['assign']] = store_schema

    elif op == 'STORE_FIND_BY':
        lines.extend(_compile_store_find_by_csharp(inst, indent, ctx))

    elif op == 'STORE_FILTER':
        lines.extend(_compile_store_filter_csharp(inst, indent, ctx))

    elif op == 'STORE_INSERT':
        assign = inst['assign']
        data = inst['data']
        locals_dict = ctx.setdefault('locals', {})
        data_type = locals_dict.get(data)
        store_schema = (ctx.get('store_def') or {}).get('schema', '')
        # When `data` is already a full entity (from CONSTRUCT of the store's
        # schema), the store's Insert(Dto) won't accept it. For in-memory
        # stores, call Seed() to persist the full entity as-is (keeps the
        # caller-supplied PK). READ_BODY-result DTOs share the same locals
        # registration but are flagged in `_dto_vars` — route those through
        # the regular Insert(dto) path. PG always calls Insert.
        dto_vars = ctx.get('_dto_vars') or set()
        if not is_pg and data_type == store_schema and data_type and data not in dto_vars:
            lines.append(f"{pad}{store_var}.Seed(new[] {{ {data} }});")
            if assign != data:
                if assign in locals_dict:
                    lines.append(f"{pad}{assign} = {data};")
                else:
                    lines.append(f"{pad}var {assign} = {data};")
                    locals_dict[assign] = data_type
        elif is_pg:
            lines.append(f"{pad}var {assign} = await {store_var}.InsertAsync({data});")
            locals_dict[assign] = store_schema
        else:
            decl = assign if assign in locals_dict else f"var {assign}"
            lines.append(f"{pad}{decl} = {store_var}.Insert({data});")
            locals_dict[assign] = store_schema

    elif op == 'STORE_UPDATE':
        if is_pg:
            lines.append(f"{pad}var {inst['assign']} = await {store_var}.UpdateAsync({inst['key']}, {inst['data']});")
        else:
            lines.append(f"{pad}var {inst['assign']} = {store_var}.Update({inst['key']}, {inst['data']});")

    elif op == 'STORE_UPDATE_FIELDS':
        key = inst.get('key', 'id')
        assign = inst.get('assign')
        fields = inst.get('fields', {})
        store_schema = (ctx.get('store_def') or {}).get('schema', 'object')
        schema_def = (ctx.get('schemas') or {}).get(store_schema, {})
        schema_fields = schema_def.get('fields', {})
        key_expr = _cs_expr(key, ctx)
        update_parts = []
        for f, v in fields.items():
            ftype = (schema_fields.get(f) or {}).get('type')
            val = _cs_expr(v, ctx, hint=ftype)
            if ftype in ('enum', 'string') and isinstance(v, str):
                known = (ctx.get('locals') or {})
                if v not in known and not v.startswith('"') and '.' not in v:
                    val = f'"{v}"'
            update_parts.append(f'{pascal_case(f)} = {val}')
        updates = ", ".join(update_parts)
        # Find existing → clone with `with { }` → persist via Seed() so the
        # record stays in the store for any follow-up STORE_UPDATE_FIELDS /
        # STORE_FIND in the same handler.
        locals_dict = ctx.setdefault('locals', {})
        if assign and assign not in locals_dict:
            lines.append(f"{pad}{store_schema}? {assign} = null;")
            locals_dict[assign] = store_schema
        lines.append(f"{pad}{{")
        lines.append(f"{pad}    var _existing = {store_var}.Find({key_expr});")
        lines.append(f"{pad}    if (_existing != null)")
        lines.append(f"{pad}    {{")
        lines.append(f"{pad}        var _updated = _existing with {{ {updates} }};")
        lines.append(f"{pad}        {store_var}.Seed(new[] {{ _updated }});")
        if assign:
            lines.append(f"{pad}        {assign} = _updated;")
        lines.append(f"{pad}    }}")
        lines.append(f"{pad}}}")

    elif op == 'STORE_DELETE':
        if is_pg:
            lines.append(f"{pad}await {store_var}.DeleteAsync({inst['key']});")
        else:
            lines.append(f"{pad}{store_var}.Delete({inst['key']});")

    elif op == 'SQL_QUERY':
        lines.extend(_compile_sql_query_csharp(inst, indent, ctx))

    elif op == 'SQL_STREAM':
        lines.extend(_compile_sql_stream_csharp(inst, indent, ctx))

    elif op == 'SQL_EXEC':
        lines.extend(_compile_sql_exec_csharp(inst, indent, ctx))

    elif op == 'STREAM_RESPOND':
        lines.extend(_compile_stream_respond_csharp(inst, indent, ctx))

    elif op == 'READ_PARAM':
        # Route-typed params (uuid/int) are bound as string with suffix "__raw" in the
        # minimal-API signature to avoid shadowing the target assign variable.
        ptype = inst.get('type', 'string')
        name = inst['name']
        assign = inst['assign']
        if ptype == 'uuid':
            lines.append(f'{pad}if (!Guid.TryParse({name}__raw, out var {assign}_tmp))')
            lines.append(f'{pad}    return Results.BadRequest(AiLangError.InvalidParam("{name}", "uuid"));')
            lines.append(f"{pad}var {assign} = {assign}_tmp;")
        elif ptype == 'int':
            lines.append(f'{pad}if (!int.TryParse({name}__raw, out var {assign}_tmp))')
            lines.append(f'{pad}    return Results.BadRequest(AiLangError.InvalidParam("{name}", "int"));')
            lines.append(f"{pad}var {assign} = {assign}_tmp;")
        else:
            if assign != name:
                lines.append(f"{pad}var {assign} = {name};")

    elif op == 'READ_QUERY':
        name = inst['name']
        ptype = inst.get('type', 'string')
        assign = inst['assign']
        default = inst.get('default')
        cs_type = {'int': 'int', 'string': 'string', 'bool': 'bool'}.get(ptype, 'string')
        if ptype == 'int':
            default_lit = int(default) if default is not None else 0
            lines.append(
                f'{pad}var {assign} = int.TryParse(ctx.Request.Query["{name}"], out var {assign}_q) ? {assign}_q : {default_lit};'
            )
        elif ptype == 'bool':
            default_lit = 'true' if default in (True, 'true') else 'false'
            lines.append(
                f'{pad}var {assign} = bool.TryParse(ctx.Request.Query["{name}"], out var {assign}_q) ? {assign}_q : {default_lit};'
            )
        else:
            default_lit = f'"{default}"' if default is not None else '""'
            lines.append(
                f'{pad}var {assign} = ctx.Request.Query["{name}"].ToString() is {{ Length: > 0 }} {assign}_q ? {assign}_q : {default_lit};'
            )

    elif op == 'READ_BODY':
        # AILang contract: READ_BODY runs schema validations (required/min/max/gt/…)
        # automatically. Generator enforces this by calling Validate() right after
        # JSON decode — mirrors the behavior described in CLAUDE.md.
        # JsonException on missing required props / malformed JSON → 400 instead
        # of the ASP.NET default 500. Handlers may still fail later in the
        # pipeline; only the deserialization step is protected here.
        schema = inst['schema']
        assign = inst['assign']
        lines.append(f"{pad}{schema}Dto? {assign} = null;")
        if ctx.get('aot'):
            service_pascal = pascal_case(ctx['service_name'])
            lines.append(f"{pad}try {{ {assign} = await ctx.Request.ReadFromJsonAsync({service_pascal}JsonContext.Default.{schema}Dto); }}")
        else:
            lines.append(f"{pad}try {{ {assign} = await ctx.Request.ReadFromJsonAsync<{schema}Dto>(); }}")
        lines.append(f"{pad}catch (System.Text.Json.JsonException _je)")
        lines.append(f"{pad}{{ return Results.BadRequest(AiLangError.InvalidBody(\"{schema}\", _je.Message)); }}")
        lines.append(f"{pad}if ({assign} is null)")
        lines.append(f"{pad}    return Results.BadRequest(AiLangError.InvalidBody(\"{schema}\", \"body is null\"));")
        lines.append(f"{pad}{{")
        lines.append(f"{pad}    var _verr = {assign}.Validate();")
        lines.append(f"{pad}    if (_verr != null) return Results.BadRequest(_verr);")
        lines.append(f"{pad}}}")
        # Track as DTO so STORE_INSERT pass-through skips it. Also register
        # schema so BRANCH field-type lookups work.
        ctx.setdefault('locals', {})[assign] = schema
        ctx.setdefault('_dto_vars', set()).add(assign)

    elif op == 'RESPOND':
        status = inst.get('status', 200)
        body = inst.get('body')
        if status == 204 or body is None:
            lines.append(f"{pad}return Results.NoContent();")
        elif status == 201:
            lines.append(f'{pad}return Results.Created($"/", {body});')
        else:
            lines.append(f"{pad}return Results.Ok({body});")

    elif op == 'RESPOND_STATUS':
        lines.append(f"{pad}return Results.StatusCode({inst['status']});")

    elif op in ('ERR_NOT_FOUND', 'ERR_VALIDATION', 'ERR_BUSINESS_RULE',
                'ERR_INVALID_BODY', 'ERR_INVALID_PARAM', 'ERR_UNAUTHORIZED',
                'ERR_UNAVAILABLE', 'ERR_TIMEOUT'):
        lines.extend(compile_error_csharp(inst, pad, ctx))

    elif op == 'ERR_CUSTOM':
        err_type = inst['type']
        params = inst.get('params', {}) or {}
        custom_errors = ctx.get('custom_errors', {}) or {}
        entry = custom_errors.get(err_type)
        type_info_arg = (
            f", {pascal_case(ctx['service_name'])}JsonContext.Default.AiLangError"
            if ctx.get('aot') else ""
        )
        if entry:
            factory = err_name_to_factory(err_type)
            declared = entry.get('params', []) or []
            status = entry.get('http_status', 422)
            # Pass params in the declared order so the factory signature matches.
            # Use _cs_expr so JsonNode-typed roots get indexer access (Phase 3b).
            args = [_cs_expr(params.get(p, ""), ctx) for p in declared]
            lines.append(f'{pad}return Results.Json(AiLangError.{factory}({", ".join(args)}){type_info_arg}, statusCode: {status});')
        else:
            tag = err_type_tag(err_type)
            lines.append(f'{pad}return Results.Json(AiLangError.BusinessRule("{tag}", ""){type_info_arg}, statusCode: 422);')

    elif op == 'SAGA_COMPENSATE':
        saga = inst.get('saga', '')
        fn = 'Compensate' + pascal_case(saga) + 'Async'
        lines.append(f"{pad}await {fn}();")

    elif op == 'PARALLEL':
        lines.extend(_compile_parallel_csharp(inst, indent, ctx))

    elif op == 'AWAIT_ALL':
        lines.extend(_compile_await_all_csharp(inst, indent, ctx))

    elif op == 'CALL_SERVICE':
        service = inst['service']
        method = inst.get('method', 'GET').upper()
        # Path interpolation: resolve {var.field} via _cs_expr for JsonNode awareness.
        import re
        raw_path = inst.get('path', '')
        def _resolve_path_segment(m):
            expr_str = m.group(1)
            resolved = _cs_expr(expr_str, ctx)
            return '{' + resolved + '}'
        path_expr = '"' + re.sub(r'\{([^}]+)\}', _resolve_path_segment, raw_path) + '"'
        if '{' in path_expr:
            path_expr = '$' + path_expr
        assign = inst['assign']
        body = inst.get('body')
        body_expr = 'null'
        if body:
            pairs = ", ".join(f'["{k}"] = {_cs_expr(v, ctx)}' for k, v in body.items())
            body_expr = f'new System.Collections.Generic.Dictionary<string, object?> {{ {pairs} }}'

        # Pick the resilience variant. When idempotent=false and the policy
        # defines a retry strategy, route through the "_no_retry" sibling client
        # so the retry is bypassed for mutations — per AILang contract.
        idempotent = inst.get('idempotent', True)
        svc_defs = ctx.get('service_refs') or {}
        policies = ctx.get('resilience_policies') or {}
        policy_name = (svc_defs.get(service) or {}).get('resilience')
        policy = policies.get(policy_name) if policy_name else None
        has_retry = bool(policy and policy.get('retry'))
        client_name = f"{service}_no_retry" if (has_retry and not idempotent) else service

        # When the service has a resilience policy AND we're at handler scope
        # (not inside a PARALLEL fiber), surface Polly-specific failures
        # (timeout / circuit-open) as 504 / 503 before the MATCH step kicks
        # in. Inside a fiber the return type is `Task<JsonNode?>` so IResult
        # can't be returned — swallow the exception as null and let the outer
        # AWAIT_ALL + MATCH NONE arm decide (graceful degradation).
        in_fiber = ctx.get('_in_parallel_fiber', False)
        if policy and not in_fiber:
            lines.append(f'{pad}System.Text.Json.Nodes.JsonNode? {assign};')
            lines.append(f'{pad}try')
            lines.append(f'{pad}{{')
            lines.append(
                f'{pad}    {assign} = await CallServiceAsync(httpClientFactory, "{client_name}", "{method}", {path_expr}, {body_expr});'
            )
            lines.append(f'{pad}}}')
            type_info_arg = (
                f", {pascal_case(ctx['service_name'])}JsonContext.Default.AiLangError"
                if ctx.get('aot') else ""
            )
            lines.append(f'{pad}catch (Polly.Timeout.TimeoutRejectedException)')
            lines.append(f'{pad}{{ return Results.Json(AiLangError.Timeout("{service}"){type_info_arg}, statusCode: 504); }}')
            lines.append(f'{pad}catch (Polly.CircuitBreaker.BrokenCircuitException)')
            lines.append(f'{pad}{{ return Results.Json(AiLangError.Unavailable("{service}"){type_info_arg}, statusCode: 503); }}')
        elif policy and in_fiber:
            lines.append(f'{pad}System.Text.Json.Nodes.JsonNode? {assign} = null;')
            lines.append(f'{pad}try {{ {assign} = await CallServiceAsync(httpClientFactory, "{client_name}", "{method}", {path_expr}, {body_expr}); }}')
            lines.append(f'{pad}catch (Polly.Timeout.TimeoutRejectedException) {{ /* fiber: null → MATCH NONE */ }}')
            lines.append(f'{pad}catch (Polly.CircuitBreaker.BrokenCircuitException) {{ /* fiber: null → MATCH NONE */ }}')
        else:
            lines.append(
                f'{pad}System.Text.Json.Nodes.JsonNode? {assign} = await CallServiceAsync(httpClientFactory, "{client_name}", "{method}", {path_expr}, {body_expr});'
            )
        ctx.setdefault('locals', {})[assign] = 'JsonNode'

    elif op == 'MATCH':
        lines.extend(compile_match_csharp(inst, indent, ctx))

    elif op == 'BRANCH':
        lines.extend(compile_branch_csharp(inst, indent, ctx))

    elif op == 'EMIT_EVENT':
        data_expr = _cs_value(inst.get('data')) if inst.get('data') not in (None, '') else 'null'
        lines.append(f'{pad}eventBus.Publish("{inst["event"]}", {data_expr});')

    elif op == 'DEFINE_VAR':
        lines.extend(_compile_define_var_csharp(inst, pad, ctx))

    elif op == 'SET_VAR':
        name = inst['name']
        hint = (ctx.get('locals') or {}).get(name)
        val_raw = inst.get('value')
        val = _cs_expr(val_raw, ctx, hint=hint)
        # String/enum vars: quote bare atoms that aren't known locals.
        if hint in ('string', 'enum') and isinstance(val_raw, str):
            known = (ctx.get('locals') or {})
            if val_raw not in known and not val_raw.startswith('"') and '.' not in val_raw:
                try: float(val_raw)
                except ValueError: val = f'"{val_raw}"'
        lines.append(f"{pad}{name} = {val};")

    elif op == 'COMPUTE':
        operator = inst.get('operator', '+')
        assign = inst['assign']
        # For arithmetic, hint operands as decimal by default (AILang's money default).
        # For comparisons, pull the type from the non-dotted operand when possible.
        arith_ops = {'+', '-', '*', '/', '%'}
        cmp_ops = {'==', '!=', '<', '<=', '>', '>=' }
        hint_left = hint_right = None
        def _is_json_path_cs(v):
            if not (isinstance(v, str) and '.' in v):
                return False
            root = v.split('.')[0]
            return (ctx.get('locals') or {}).get(root) == 'JsonNode'
        if operator in arith_ops:
            left_raw, right_raw = inst.get('left'), inst.get('right')
            if _is_json_path_cs(left_raw) or _is_json_path_cs(right_raw):
                hint_left = hint_right = 'decimal'
        elif operator in cmp_ops:
            left_raw, right_raw = inst.get('left'), inst.get('right')
            hint_left = _scalar_type_hint(right_raw)
            hint_right = _scalar_type_hint(left_raw)
        left = _cs_expr(inst.get('left'), ctx, hint=hint_left)
        right = _cs_expr(inst.get('right'), ctx, hint=hint_right)
        locals_dict = ctx.setdefault('locals', {})
        if assign in locals_dict:
            lines.append(f"{pad}{assign} = {left} {operator} {right};")
        else:
            lines.append(f"{pad}var {assign} = {left} {operator} {right};")
            locals_dict[assign] = 'inferred'

    elif op == 'COMPUTE_FN':
        lines.extend(_compile_compute_fn_csharp(inst, pad, ctx))

    elif op == 'TERNARY':
        cond = _cs_value(inst.get('condition'))
        then_v = _cs_value(inst.get('then'))
        else_v = _cs_value(inst.get('else'))
        lines.append(f"{pad}var {inst['assign']} = {cond} ? {then_v} : {else_v};")

    elif op == 'IS_OK':
        val = inst.get('value')
        lines.append(f"{pad}var {inst['assign']} = {val} is not null;")

    elif op == 'APPEND':
        target = inst['target']
        if 'value' in inst:
            lines.append(f"{pad}{target}.Add({_cs_expr(inst['value'], ctx)});")
        else:
            locals_dict = ctx.get('locals', {}) or {}
            ttype = locals_dict.get(target, 'List<object>')
            elem_name = extract_list_elem(ttype) if ttype else 'object'
            elem_cs = extract_list_elem(ailang_type_to_csharp(ttype))
            elem_schema = (ctx.get('schemas') or {}).get(elem_name, {})
            elem_fields = elem_schema.get('fields', {})
            fields = inst.get('object_fields', {})
            parts = []
            for k, v in fields.items():
                ftype = (elem_fields.get(k) or {}).get('type')
                val = _cs_expr(v, ctx, hint=ftype)
                # AILang convention in APPEND: dotted values are variable refs,
                # bare identifiers in a string/enum slot are atoms — even when
                # the atom shadows a local var name (e.g. `(type coupon)` with
                # `coupon` also being the current local var).
                if ftype in ('enum', 'string') and isinstance(v, str) \
                        and not v.startswith('"') and '.' not in v:
                    try:
                        float(v)
                    except ValueError:
                        val = f'"{v}"'
                parts.append(f"{pascal_case(k)} = {val}")
            init = ", ".join(parts)
            lines.append(f"{pad}{target}.Add(new {elem_cs} {{ {init} }});")

    elif op == 'ITERATE':
        collection = inst['collection']
        item = inst['item']
        # Resolve the collection path: dotted paths get PascalCase for typed records
        # or indexer access for JsonNode roots.
        coll_root = collection.split('.')[0] if '.' in collection else collection
        coll_type = (ctx.get('locals') or {}).get(coll_root) if ctx else None
        coll_expr = _cs_expr(collection, ctx)
        if coll_type == 'JsonNode':
            lines.append(f"{pad}foreach (var {item} in {coll_expr}!.AsArray())")
            item_type = 'JsonNode'
        else:
            lines.append(f"{pad}foreach (var {item} in {coll_expr})")
            # Infer the element type from the collection's schema field.
            # `body.items` where body is CreateOrderRequest → items is
            # (list CreateOrderItem) → item is CreateOrderItem (typed record).
            # Falls back to 'record' (typed but unknown schema) so dotted
            # access PascalCases instead of dict-indexing.
            item_type = 'record'
            if isinstance(collection, str) and '.' in collection:
                root, _, leaf = collection.rpartition('.')
                root_first = root.split('.')[0]
                root_type_name = (ctx.get('locals') or {}).get(root_first)
                if root_type_name:
                    schema_def = (ctx.get('schemas') or {}).get(root_type_name, {})
                    fdef = schema_def.get('fields', {}).get(leaf, {})
                    if fdef.get('type') == 'list':
                        item_type = fdef.get('of') or 'record'
        lines.append(f"{pad}{{")
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        inner_ctx['locals'][item] = item_type
        for sub in inst.get('body', []):
            lines.extend(compile_instruction_csharp(sub, indent + 1, inner_ctx))
        lines.append(f"{pad}}}")

    elif op == 'CONSTRUCT':
        schema = inst['schema']
        fields = inst.get('fields', {})
        assign = inst['assign']
        if schema == 'object':
            # Anonymous object emitted as `Dictionary<string, object?>` so
            # snake_case keys round-trip through JSON without the default
            # camelCase property-naming policy mangling them. C#'s anonymous
            # type would PascalCase the members and the serializer would then
            # lowercase-first them — `product_service` → `productService`.
            # Bare atoms that aren't known locals are treated as string
            # literals; nested op-forms (TERNARY, IS_OK) resolve inline.
            known = (ctx.get('locals') or {})
            parts = []
            for k, v in fields.items():
                val = _cs_construct_object_value(v, ctx, known)
                parts.append(f'["{k}"] = {val}')
            body = ", ".join(parts)
            locals_dict = ctx.setdefault('locals', {})
            if assign in locals_dict:
                lines.append(f"{pad}{assign} = new Dictionary<string, object?> {{ {body} }};")
            else:
                lines.append(f"{pad}object {assign} = new Dictionary<string, object?> {{ {body} }};")
                locals_dict[assign] = 'object'
        else:
            schema_def = (ctx.get('schemas') or {}).get(schema, {})
            schema_fields = schema_def.get('fields', {})
            parts = []
            # Auto-fill auto_gen fields that aren't explicitly provided.
            for sf_name, sf_def in schema_fields.items():
                if sf_def.get('auto_gen') and sf_name not in fields:
                    parts.append(f"{pascal_case(sf_name)} = {_autogen_default_csharp(sf_def['type'])}")
            for k, v in fields.items():
                fdef = schema_fields.get(k) or {}
                field_type = fdef.get('type')
                # Nested `object`-typed field with a pair-list value → emit as
                # Dictionary<string, object?>. Mirrors the schema=='object' path.
                if field_type == 'object' and isinstance(v, list) and v:
                    known = (ctx.get('locals') or {})
                    val = _cs_construct_object_value(v, ctx, known)
                else:
                    val = _cs_expr(v, ctx, hint=field_type)
                    if field_type in ('enum', 'string') and isinstance(v, str):
                        known = (ctx.get('locals') or {})
                        if v not in known and not v.startswith('"') and '.' not in v:
                            val = f'"{v}"'
                parts.append(f"{pascal_case(k)} = {val}")
            body = ", ".join(parts)
            lines.append(f"{pad}var {assign} = new {schema} {{ {body} }};")
            ctx.setdefault('locals', {})[assign] = schema

    elif op == 'GET_CURRENT':
        from_ = inst.get('from', 'auth')
        field = inst.get('field', 'user')
        assign = inst['assign']
        if from_ == 'auth' and field == 'user':
            lines.append(f'{pad}var {assign} = ctx.User.FindFirst("sub")?.Value ?? "";')
        elif from_ == 'clock' and field == 'now':
            lines.append(f"{pad}var {assign} = clock.UtcNow();")
            ctx.setdefault('locals', {})[assign] = 'timestamp'
        elif from_ == 'clock' and field == 'today':
            lines.append(f"{pad}var {assign} = clock.Today();")
            ctx.setdefault('locals', {})[assign] = 'timestamp'
        elif from_ == 'clock' and field == 'day_of_week':
            lines.append(f"{pad}var {assign} = clock.DayOfWeek();")
            ctx.setdefault('locals', {})[assign] = 'day_of_week'
        else:
            lines.append(f"{pad}var {assign} = default!;  // GET_CURRENT {from_}.{field} (unsupported)")

    elif op == 'LOG':
        level = inst.get('level', 'INFO')
        import re
        msg = inst['message']
        # Resolve dotted paths inside {interpolation} braces via _cs_expr so
        # JsonNode roots get indexer access and typed roots get PascalCase.
        msg = re.sub(r'\{(\w+(?:\.\w+)+)\}', lambda m: '{' + _cs_expr(m.group(1), ctx) + '}', msg)
        lines.append(f'{pad}Console.WriteLine($"[{level}] {msg}");')

    return [l for l in lines if l != ""]


def _scalar_type_hint(x):
    """Infer the AILang type of a scalar value for Phase 3b cast hinting.

    Returns 'bool', 'int', 'decimal', 'string', or None when the value is
    an identifier or complex form. Used by COMPUTE, BRANCH, and MATCH to
    decide the GetValue<T>() cast on JsonNode leaves."""
    from sexpr_parser import StringLiteral
    if isinstance(x, bool):
        return 'bool'
    if isinstance(x, int):
        return 'int'
    if isinstance(x, float):
        return 'decimal'
    if isinstance(x, StringLiteral):
        return 'string'
    if isinstance(x, str) and x.lower() in ('true', 'false'):
        return 'bool'
    return None


def _cs_json_cast(expr: str, hint: str) -> str:
    """Wrap a JsonNode expression with GetValue<T>() for the requested C# type.

    Returns the untouched expression when hint is None or unknown — useful when
    passing a JsonNode? through to another generic sink (e.g. nested JsonNode
    access or a helper that already takes JsonNode)."""
    if not hint:
        return expr
    # Hint comes from AILang field type; map to a CLR type the JsonNode API knows.
    cs_type = {
        'uuid': 'string',
        'string': 'string',
        'int': 'int',
        'decimal': 'decimal',
        'bool': 'bool',
        'timestamp': 'DateTime',
    }.get(hint)
    if not cs_type:
        return expr  # list / object / unknown — leave as JsonNode
    return f'({expr})!.GetValue<{cs_type}>()'


def _cs_resolve_path(expr: str, ctx: dict, hint: str = None) -> str:
    """Resolve a dotted identifier (`root.a.b`) to the appropriate C# expression.

    - If the root is a JsonNode (set by CALL_SERVICE), emit indexer access
      (`root["a"]!["b"]!`) and cap with `GetValue<T>()` when a hint is given.
    - Otherwise fall back to member access with PascalCased segments
      (the existing typed-record behavior).
    """
    if not isinstance(expr, str) or '.' not in expr:
        return expr
    parts = expr.split('.')
    if not parts or not (parts[0] and (parts[0][0].isalpha() or parts[0][0] == '_')):
        return expr
    root_type = (ctx.get('locals') or {}).get(parts[0]) if ctx else None
    if root_type == 'JsonNode':
        inner = parts[0]
        for seg in parts[1:]:
            inner += f'["{seg}"]'
        # Cap trailing `!` only when reading a leaf value — the hint cast already dereferences.
        if hint and hint not in ('list', 'object'):
            return _cs_json_cast(inner, hint)
        # No hint: caller wants a JsonNode? — stay null-safe, no trailing `!`.
        return inner
    # Object-typed vars (from CONSTRUCT object, now emitted as
    # `Dictionary<string, object?>`) use indexer access so snake_case keys
    # work and no RuntimeBinder lookup is needed. `((IDictionary<…>)root)["k"]`
    # returns object?; a string/numeric hint adds the appropriate cast.
    if root_type == 'object':
        inner = parts[0]
        for p in parts[1:]:
            inner = f'((System.Collections.Generic.IDictionary<string, object?>){inner})["{p}"]'
        if hint in ('string', 'enum', 'uuid'):
            return f'((string?){inner})'
        if hint == 'bool':
            return f'((bool)({inner} ?? false))'
        if hint == 'int':
            return f'Convert.ToInt32({inner})'
        if hint == 'decimal':
            return f'Convert.ToDecimal({inner})'
        return inner
    # Typed access — same rule as before.
    return parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])


def _cs_construct_object_value(v, ctx: dict, known: dict) -> str:
    """Render a CONSTRUCT-object field value as a C# expression.

    Scalars and identifiers fall through to _cs_expr with string-atom quoting
    (same behavior the old emitter had). A list-of-pairs whose first pair is
    `['op', 'TERNARY']` or `['op', 'IS_OK']` is an inline op form captured by
    the sexpr parser — rebuild the op dict and emit the expression directly so
    the enclosing object literal doesn't need a separate statement.
    """
    # Pair-list: either an inline op form or a plain nested object. If the
    # first key is `op`, treat as an inline TERNARY/IS_OK expression. Else
    # emit as `Dictionary<string, object?>` so snake_case keys round-trip.
    if isinstance(v, list) and v and all(isinstance(p, list) and len(p) >= 2 for p in v):
        op_dict = {}
        for pair in v:
            key = str(pair[0])
            op_dict[key] = pair[1] if len(pair) == 2 else pair[1:]
        if op_dict.get('op') == 'TERNARY':
            cond_expr = _cs_construct_object_value(op_dict.get('condition'), ctx, known)
            then_expr = _cs_construct_object_value(op_dict.get('true'), ctx, known)
            else_expr = _cs_construct_object_value(op_dict.get('false'), ctx, known)
            return f'({cond_expr} ? (object){then_expr} : (object){else_expr})'
        if op_dict.get('op') == 'IS_OK':
            left = op_dict.get('left') or op_dict.get('value')
            left_expr = _cs_construct_object_value(left, ctx, known)
            return f'({left_expr} is not null)'
        # Plain nested object — emit as Dictionary.
        parts = []
        for pair in v:
            k = str(pair[0])
            inner_val = pair[1] if len(pair) == 2 else pair[1:]
            parts.append(f'["{k}"] = {_cs_construct_object_value(inner_val, ctx, known)}')
        return 'new Dictionary<string, object?> { ' + ', '.join(parts) + ' }'
    # Scalar / identifier path.
    val = _cs_expr(v, ctx)
    if isinstance(v, str) and v not in known and not v.startswith('"') and '.' not in v:
        try:
            float(v)
        except ValueError:
            if v.lower() not in ('true', 'false', 'null', 'nil'):
                val = f'"{v}"'
    return val


def _cs_expr(v, ctx: dict = None, hint: str = None) -> str:
    """Context-aware value formatter. Delegates to _cs_value for scalars/literals
    but routes dotted identifiers through _cs_resolve_path so JsonNode-typed
    roots get indexer access instead of pascalized member access.

    When hint is 'string' or 'enum' and the value is a bare atom that isn't
    a known local variable, it's treated as a literal string and quoted."""
    from sexpr_parser import StringLiteral
    if ctx and isinstance(v, str) and not isinstance(v, StringLiteral) and '.' in v:
        s = v
        if s and (s[0].isalpha() or s[0] == '_') and all(c.isalnum() or c in '_.' for c in s):
            return _cs_resolve_path(s, ctx, hint)
    result = _cs_value(v)
    # Centralized bare-atom quoting for string/enum hints.
    if hint in ('string', 'enum') and ctx and isinstance(v, str) and not isinstance(v, StringLiteral):
        known = (ctx.get('locals') or {})
        if v not in known and not v.startswith('"') and '.' not in v:
            try:
                float(v)
            except ValueError:
                if v.lower() not in ('true', 'false', 'null', 'nil'):
                    result = f'"{v}"'
    return result


def _cs_value(v) -> str:
    """Format an AILang value for inline use in C# — pass-through for identifiers,
    escape strings, respect bool/numeric/None. Identifiers containing `.` are
    interpreted as member access; each segment after the first gets PascalCase."""
    from sexpr_parser import StringLiteral
    if v is None:
        return 'null'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    # StringLiteral must come before generic str checks since it's a str subclass.
    if isinstance(v, StringLiteral):
        escaped = str(v).replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return f'(object)null /* TODO nested: list value */'
    s = str(v)
    if s.startswith('"'):
        return s
    try:
        float(s)
        return s
    except ValueError:
        pass
    # Identifier or member access: only alphanumerics + `_` + `.`
    if s and (s[0].isalpha() or s[0] == '_') and all(c.isalnum() or c in '_.' for c in s):
        if '.' in s:
            parts = s.split('.')
            return parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])
        return s
    escaped = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _compile_define_var_csharp(inst: dict, pad: str, ctx: dict) -> list[str]:
    from sexpr_parser import StringLiteral
    name = inst['name']
    vtype = inst.get('type', 'object')
    value = inst.get('value')
    cs_type = ailang_type_to_csharp(vtype)
    ctx.setdefault('locals', {})[name] = vtype

    if isinstance(value, dict) and value.get('__empty_list__'):
        return [f"{pad}{cs_type} {name} = [];"]
    if value is None or str(value) == 'nil':
        return [f"{pad}{cs_type} {name} = default!;"]
    if isinstance(value, bool):
        return [f"{pad}{cs_type} {name} = {'true' if value else 'false'};"]
    # StringLiteral is a str subclass, so this branch must run BEFORE the int/float
    # branches to avoid picking up strings that happen to parse as numbers.
    if isinstance(value, StringLiteral):
        return [f"{pad}{cs_type} {name} = {_cs_value(value)};"]
    if isinstance(value, int):
        suffix = "m" if vtype == 'decimal' else ""
        return [f"{pad}{cs_type} {name} = {value}{suffix};"]
    if isinstance(value, float):
        return [f"{pad}{cs_type} {name} = {value}m;" if vtype == 'decimal' else f"{pad}{cs_type} {name} = {value};"]
    s = str(value)
    if s.startswith('"'):
        return [f"{pad}{cs_type} {name} = {s};"]
    # When the declared type is `string`, treat a bare atom as a string literal
    # (the parser couldn't tell — e.g. `(DEFINE_VAR x string cancelled)` with
    # the quotes stripped upstream).
    if vtype in ('string', 'enum'):
        # Bare atom in string/enum context → always quote as string literal.
        v = _cs_value(value)
        if not v.startswith('"') and not v.startswith("'"):
            v = f'"{value}"'
        return [f"{pad}{cs_type} {name} = {v};"]
    if vtype == 'object' and s.startswith('{'):
        return [f"{pad}{cs_type} {name} = default!;  // TODO: object initializer {s}"]
    return [f"{pad}{cs_type} {name} = {s};"]


def _compile_compute_fn_csharp(inst: dict, pad: str, ctx: dict) -> list[str]:
    fn = inst['function']
    raw_args = inst.get('args', [])
    args = [_cs_value(a) for a in raw_args]
    assign = inst['assign']
    locals_dict = ctx.setdefault('locals', {})
    # If the target was already declared (DEFINE_VAR), emit assignment; else `var`.
    decl = assign if assign in locals_dict else f"var {assign}"
    fn_map = {
        'round': 'Math.Round', 'floor': 'Math.Floor', 'ceil': 'Math.Ceiling',
        'min': 'Math.Min', 'max': 'Math.Max', 'abs': 'Math.Abs',
    }
    if fn == 'length':
        src_raw = raw_args[0] if raw_args else ''
        src = args[0] if args else 'default'
        src_type = locals_dict.get(str(src_raw))
        if src_type in ('JsonNode', 'any_json'):
            return [f"{pad}{decl} = {src}!.AsArray().Count;"]
        return [f"{pad}{decl} = {src}.Count;"]
    if fn == 'take_last':
        src = args[0] if args else 'default'
        n = args[1] if len(args) > 1 else '5'
        return [f"{pad}{decl} = {src}!.AsArray().Skip(Math.Max(0, {src}!.AsArray().Count - {n})).ToList();"]
    if fn in fn_map:
        return [f"{pad}{decl} = {fn_map[fn]}({', '.join(args)});"]
    return [f"{pad}{decl} = default!;  // COMPUTE_FN: unsupported function {fn}"]


def _pg_columns_ordered(store_def: dict) -> list[tuple[str, dict]]:
    """Return (field, mapping_entry) pairs in declaration order."""
    return list((f, m) for f, m in store_def.get('mapping', {}).items())


def _pg_select_columns_sql(store_def: dict) -> str:
    cols = [m['column'] for _, m in _pg_columns_ordered(store_def)]
    return ", ".join(cols)


def _compile_store_find_by_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "    " * indent
    store_def = ctx['store_def']
    store_var = ctx['store_var']
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    where_sql = where_to_sql(inst.get('where'))
    cols_sql = _pg_select_columns_sql(store_def)
    sql = f'SELECT {cols_sql}\nFROM {table}\nWHERE {where_sql}\nLIMIT 1'
    assign = inst['assign']
    sql_lit = emit_sql_literal_csharp(sql, indent + 1)
    lines = [
        f"{pad}{schema}? {assign} = null;",
        f"{pad}{{",
        f'{pad}    await using var _conn = await {store_var}.DataSource.OpenConnectionAsync();',
        f'{pad}    await using var _cmd = new Npgsql.NpgsqlCommand({sql_lit}, _conn);',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}    _cmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f"{pad}    await using var _reader = await _cmd.ExecuteReaderAsync();")
    lines.append(f"{pad}    if (await _reader.ReadAsync())")
    lines.append(f"{pad}    {{")
    lines.append(f"{pad}        {assign} = Postgres{schema}Store.MapRow(_reader);")
    lines.append(f"{pad}    }}")
    lines.append(f"{pad}}}")
    return lines


def _compile_store_filter_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "    " * indent
    store_def = ctx['store_def']
    store_var = ctx['store_var']
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    where_sql = where_to_sql(inst.get('where'))
    cols_sql = _pg_select_columns_sql(store_def)
    assign = inst['assign']
    paginate = inst.get('paginate') or {}
    page_var = paginate.get('page') or 1
    limit_var = paginate.get('limit') or 20
    max_limit = paginate.get('max_limit') or 100

    count_sql = f'SELECT COUNT(*)\nFROM {table}\nWHERE {where_sql}'
    data_sql = (
        f'SELECT {cols_sql}\n'
        f'FROM {table}\n'
        f'WHERE {where_sql}\n'
        f'ORDER BY id\n'
        f'LIMIT @_limit OFFSET @_offset'
    )
    count_lit = emit_sql_literal_csharp(count_sql, indent + 2)
    data_lit = emit_sql_literal_csharp(data_sql, indent + 2)

    lines = [
        f"{pad}Paginated<{schema}> {assign};",
        f"{pad}{{",
        f'{pad}    var _limit = Math.Clamp({limit_var}, 1, {max_limit});',
        f'{pad}    var _page  = Math.Max({page_var}, 1);',
        f'{pad}    var _offset = (_page - 1) * _limit;',
        f'{pad}    await using var _conn = await {store_var}.DataSource.OpenConnectionAsync();',
        f'{pad}    long _total;',
        f"{pad}    {{",
        f'{pad}        await using var _countCmd = new Npgsql.NpgsqlCommand({count_lit}, _conn);',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}        _countCmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f'{pad}        _total = Convert.ToInt64(await _countCmd.ExecuteScalarAsync());')
    lines.append(f"{pad}    }}")
    lines.append(f"{pad}    List<{schema}> _items = new(_limit);  // pre-alloc to page size")
    lines.append(f"{pad}    {{")
    lines.append(f'{pad}        await using var _dataCmd = new Npgsql.NpgsqlCommand({data_lit}, _conn);')
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}        _dataCmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f'{pad}        _dataCmd.Parameters.AddWithValue("@_limit", _limit);')
    lines.append(f'{pad}        _dataCmd.Parameters.AddWithValue("@_offset", _offset);')
    lines.append(f"{pad}        await using var _reader = await _dataCmd.ExecuteReaderAsync();")
    lines.append(f"{pad}        while (await _reader.ReadAsync())")
    lines.append(f"{pad}            _items.Add(Postgres{schema}Store.MapRow(_reader));")
    lines.append(f"{pad}    }}")
    lines.append(f"{pad}    var _totalPages = _limit == 0 ? 0 : (int)Math.Ceiling((double)_total / _limit);")
    lines.append(f"{pad}    {assign} = new() {{ Data = _items, Page = _page, Limit = _limit, Total = _total, TotalPages = _totalPages }};")
    lines.append(f"{pad}}}")
    return lines


def _compile_parallel_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Emit one Task per fiber + a tuple assigned to `inst['assign']` whose fields
    match fiber names. Actual awaiting happens when AWAIT_ALL compiles — this
    only spawns and stores the task handles.

    The fiber's "return value" is the assign of its LAST instruction.
    """
    pad = "    " * indent
    assign = inst['assign']
    fibers = inst.get('fibers', []) or []

    lines = [f"{pad}// ── PARALLEL {assign}: {len(fibers)} fiber(s) ──"]
    fiber_task_names = []
    for fiber in fibers:
        fname = fiber['name']
        task_var = f"_task_{assign}_{fname}"
        # Determine the fiber's "result var" from the last instruction's assign.
        # Remembered alongside the task var so MATCH on `{assign}.{fname}` can
        # rebind it in the OK arm (the result_var is otherwise lambda-local).
        result_var = None
        for f_inst in fiber.get('instructions', []):
            if f_inst.get('assign'):
                result_var = f_inst['assign']
        fiber_task_names.append((fname, task_var, result_var))

        lines.append(f"{pad}var {task_var} = Task.Run<System.Text.Json.Nodes.JsonNode?>(async () =>")
        lines.append(f"{pad}{{")
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        # Inside a fiber body the return type is `Task<JsonNode?>`, so the
        # enclosing handler's IResult shortcuts don't apply. Mark the ctx so
        # CALL_SERVICE emits the plain call without a 504/503 short-circuit —
        # the fiber swallows the exception and the outer MATCH NONE arm on
        # the fiber's result handles graceful degradation instead.
        inner_ctx['_in_parallel_fiber'] = True
        for f_inst in fiber.get('instructions', []):
            lines.extend(compile_instruction_csharp(f_inst, indent + 1, inner_ctx))
        if result_var:
            lines.append(f"{pad}    return {result_var};")
        else:
            lines.append(f"{pad}    return null;")
        lines.append(f"{pad}}});")

    # Save the task-name map in ctx so AWAIT_ALL can find it by the assign name.
    parallel_map = ctx.setdefault('_parallel_tasks', {})
    parallel_map[assign] = fiber_task_names
    ctx.setdefault('locals', {})[assign] = 'ParallelResults'
    return lines


def _compile_await_all_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Join the tasks from the matching PARALLEL with a timeout, then materialize
    a tuple variable whose fields carry each fiber's result (or null on
    failure/timeout). The MATCH OK/ERR idiom on `parallel_results.fiberName`
    works naturally because non-null = OK, null = ERR."""
    pad = "    " * indent
    source = inst['source']
    timeout_ms = inst.get('timeout_ms') or 0
    parallel_map = (ctx or {}).get('_parallel_tasks', {})
    fibers = parallel_map.get(source, [])
    if not fibers:
        return [f"{pad}// WARN: AWAIT_ALL {source} — no matching PARALLEL block"]

    task_list = ", ".join(tv for _, tv, _ in fibers)
    lines = [f"{pad}// ── AWAIT_ALL {source} (timeout {timeout_ms}ms) ──"]
    lines.append(f"{pad}try")
    lines.append(f"{pad}{{")
    if timeout_ms and timeout_ms > 0:
        lines.append(f"{pad}    await Task.WhenAll({task_list}).WaitAsync(TimeSpan.FromMilliseconds({timeout_ms}));")
    else:
        lines.append(f"{pad}    await Task.WhenAll({task_list});")
    lines.append(f"{pad}}}")
    lines.append(f"{pad}catch (Exception ex) when (ex is TimeoutException || ex is TaskCanceledException)")
    lines.append(f"{pad}{{")
    lines.append(f'{pad}    Console.WriteLine($"[PARALLEL] {source} timed out after {timeout_ms}ms — partial results");')
    lines.append(f"{pad}}}")
    # Build the tuple. Tuple fields use PascalCase so member access via
    # `_cs_resolve_path` (which PascalCases dotted segments) hits them — e.g.
    # `parallel_results.fetch_products` → `parallel_results.FetchProducts`.
    tuple_fields = ", ".join(
        f"{pascal_case(fname)}: ({tv}.Status == TaskStatus.RanToCompletion ? {tv}.Result : null)"
        for fname, tv, _ in fibers
    )
    lines.append(f"{pad}var {source} = ({tuple_fields});")
    return lines


def _compile_sql_query_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "    " * indent
    store_var = ctx['store_var']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_csharp(sql, indent + 1)
    assign = inst['assign']
    target_type = (ctx.get('returns') or {}).get('type') or 'object'
    lines = [
        f"{pad}List<{target_type}> {assign} = [];",
        f"{pad}{{",
        f'{pad}    await using var _conn = await {store_var}.DataSource.OpenConnectionAsync();',
        f'{pad}    await using var _cmd = new Npgsql.NpgsqlCommand({sql_lit}, _conn);',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}    _cmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f"{pad}    await using var _reader = await _cmd.ExecuteReaderAsync();")
    lines.append(f"{pad}    while (await _reader.ReadAsync())")
    lines.append(f"{pad}    {{")
    lines.append(f"{pad}        {assign}.Add(ReadRow{target_type}(_reader));")
    lines.append(f"{pad}    }}")
    lines.append(f"{pad}}}")
    return lines


def _compile_sql_stream_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Emit a local async iterator that yields rows as dictionaries (column-name safe)."""
    pad = "    " * indent
    store_var = ctx['store_var']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_csharp(sql, indent + 1)
    assign = inst['assign']
    row_type = "Dictionary<string, object?>"
    lines = [
        f"{pad}async IAsyncEnumerable<{row_type}> _{assign}Iter()",
        f"{pad}{{",
        f'{pad}    await using var _conn = await {store_var}.DataSource.OpenConnectionAsync();',
        f'{pad}    await using var _cmd = new Npgsql.NpgsqlCommand({sql_lit}, _conn);',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}    _cmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f"{pad}    await using var _reader = await _cmd.ExecuteReaderAsync();")
    lines.append(f"{pad}    while (await _reader.ReadAsync())")
    lines.append(f"{pad}    {{")
    lines.append(f"{pad}        var _row = new {row_type}();")
    lines.append(f"{pad}        for (int _i = 0; _i < _reader.FieldCount; _i++)")
    lines.append(f"{pad}            _row[_reader.GetName(_i)] = _reader.IsDBNull(_i) ? null : _reader.GetValue(_i);")
    lines.append(f"{pad}        yield return _row;")
    lines.append(f"{pad}    }}")
    lines.append(f"{pad}}}")
    lines.append(f"{pad}var {assign} = _{assign}Iter();")
    return lines


def _compile_sql_exec_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "    " * indent
    store_var = ctx['store_var']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_csharp(sql, indent + 1)
    assign = inst.get('assign') or '_'
    lines = [
        f"{pad}int {assign};",
        f"{pad}{{",
        f'{pad}    await using var _conn = await {store_var}.DataSource.OpenConnectionAsync();',
        f'{pad}    await using var _cmd = new Npgsql.NpgsqlCommand({sql_lit}, _conn);',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}    _cmd.Parameters.AddWithValue("@{p["name"]}", {p["var"]});')
    lines.append(f"{pad}    {assign} = await _cmd.ExecuteNonQueryAsync();")
    lines.append(f"{pad}}}")
    return lines


def _compile_stream_respond_csharp(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "    " * indent
    stream = inst.get('stream')
    status = inst.get('status', 200)
    content_type = inst.get('content_type', 'application/x-ndjson')
    # NOTE (NativeAOT): JsonSerializer.SerializeAsync(stream, value) is the
    # reflection-based overload. A service using STREAM_RESPOND under --aot
    # will trip IL2026/IL3050 at publish time. Fix when a STREAM_RESPOND
    # service needs AOT: switch to the JsonTypeInfo<T> overload sourced from
    # the JsonContext.
    lines = [
        f'{pad}ctx.Response.StatusCode = {status};',
        f'{pad}ctx.Response.ContentType = "{content_type}";',
        f'{pad}await foreach (var _item in {stream})',
        f'{pad}{{',
        f'{pad}    await System.Text.Json.JsonSerializer.SerializeAsync(ctx.Response.Body, _item);',
        f'{pad}    await ctx.Response.Body.WriteAsync(System.Text.Encoding.UTF8.GetBytes("\\n"));',
        f'{pad}}}',
        f'{pad}return Results.Empty;',
    ]
    return lines


def compile_error_csharp(inst: dict, pad: str, ctx: dict = None) -> list[str]:
    """Compile error instructions to C#.

    AOT note: Results.Json<T>() is reflection-based and trips IL2026/IL3050
    under PublishAot + TreatWarningsAsErrors. When ctx['aot'] is True we route
    these through the source-gen JsonTypeInfo<AiLangError> overload. The
    Results.NotFound / Results.BadRequest sites don't need the explicit
    JsonTypeInfo because Minimal APIs resolves through the wired
    ConfigureHttpJsonOptions chain (no IL warnings).
    """
    op = inst['op']
    lines = []
    ctx = ctx or {}
    type_info_arg = (
        f", {pascal_case(ctx['service_name'])}JsonContext.Default.AiLangError"
        if ctx.get('aot') else ""
    )

    if op == 'ERR_NOT_FOUND':
        id_expr = _cs_value(inst["id"])
        # Convert.ToString handles both value types (Guid, int) and reference
        # types — and safely returns "" for null refs — so the member access
        # doesn't need a `?.` that would reject non-nullable value types.
        lines.append(f'{pad}return Results.NotFound(AiLangError.NotFound("{inst["entity"]}", Convert.ToString({id_expr}) ?? ""));')
    elif op == 'ERR_VALIDATION':
        lines.append(f'{pad}return Results.BadRequest(AiLangError.Validation("{inst["field"]}", "{inst["reason"]}"));')
    elif op == 'ERR_BUSINESS_RULE':
        lines.append(f'{pad}return Results.Json(AiLangError.BusinessRule("{inst["rule"]}", "{inst["detail"]}"){type_info_arg}, statusCode: 422);')
    elif op == 'ERR_INVALID_BODY':
        lines.append(f'{pad}return Results.BadRequest(AiLangError.InvalidBody("{inst["schema"]}", "invalid"));')
    elif op == 'ERR_UNAUTHORIZED':
        lines.append(f'{pad}return Results.Json(AiLangError.Unauthorized(){type_info_arg}, statusCode: 401);')
    elif op == 'ERR_UNAVAILABLE':
        lines.append(f'{pad}return Results.Json(AiLangError.Unavailable("{inst.get("service", "")}"){type_info_arg}, statusCode: 503);')

    return lines


def compile_match_csharp(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    """Compile MATCH instruction to C# if/else.

    Phase 7b: when matching `{source}.{fiber}` and the source is a PARALLEL
    result, the OK branch rebinds the fiber's in-fiber result_var to the tuple
    field so downstream body code (which was written against the fiber-local
    name) still resolves. Also registers the rebound name as JsonNode.

    PARALLEL tuples now use PascalCased field names, so dotted accesses on a
    parallel-result root must PascalCase the fiber segment too.
    """
    pad = "    " * indent
    lines = []
    value_raw = inst['value']
    cases = inst.get('cases', {})

    # Detect PARALLEL-result MATCH: lookup in ctx['_parallel_tasks'] by source name.
    rebind_stmt = None
    rebind_var = None
    value = value_raw
    if isinstance(value_raw, str) and '.' in value_raw:
        source, _, fname = value_raw.partition('.')
        fibers = ((ctx or {}).get('_parallel_tasks') or {}).get(source) or []
        for fn, _tv, rv in fibers:
            if fn == fname and rv:
                rebind_var = rv
                # Access the tuple via PascalCased field (tuple_fields now
                # PascalCases every fiber name).
                pc_value = f"{source}.{pascal_case(fname)}"
                value = pc_value
                rebind_stmt = f"var {rv} = {pc_value};"
                break

    if 'OK' in cases and ('NONE' in cases or 'ERR' in cases):
        # RESULT match (find/read_body pattern) — OK/NONE or OK/ERR
        fail_case = 'NONE' if 'NONE' in cases else 'ERR'
        lines.append(f"{pad}if ({value} is null)")
        lines.append(f"{pad}{{")
        for sub in cases[fail_case]:
            lines.extend(compile_instruction_csharp(sub, indent + 1, ctx))
        lines.append(f"{pad}}}")

        if cases.get('OK'):
            has_return_in_fail = any('return' in line for line in lines)
            if not has_return_in_fail:
                lines.append(f"{pad}else")
                lines.append(f"{pad}{{")
            ok_indent = indent if has_return_in_fail else indent + 1
            ok_pad = "    " * ok_indent
            if rebind_stmt:
                lines.append(f"{ok_pad}{rebind_stmt}")
                ctx.setdefault('locals', {})[rebind_var] = 'JsonNode'
            for sub in cases['OK']:
                lines.extend(compile_instruction_csharp(sub, ok_indent, ctx))
            if not has_return_in_fail:
                lines.append(f"{pad}}}")
    else:
        # Value-dispatch MATCH (integer / string arms — not RESULT pattern).
        known = (ctx.get('locals') or {})
        all_int = all(isinstance(k, int) or (isinstance(k, str) and k.isdigit()) for k in cases if str(k) != 'DEFAULT')
        value_hint = 'int' if all_int else 'string'
        value_expr = _cs_expr(value, ctx, hint=value_hint)
        first = True
        for arm_key, arm_body in cases.items():
            arm_str = str(arm_key)
            if arm_str == 'DEFAULT':
                lines.append(f"{pad}else")
            else:
                # Arm keys are ALWAYS literal values, never variable references.
                # Always quote non-numeric arms (even if they collide with local var names).
                arm_val = _cs_value(arm_key)
                if isinstance(arm_key, str) and not arm_key.startswith('"'):
                    try: int(arm_key)
                    except ValueError: arm_val = f'"{arm_key}"'
                if first:
                    lines.append(f"{pad}if ({value_expr} == {arm_val})")
                    first = False
                else:
                    lines.append(f"{pad}else if ({value_expr} == {arm_val})")
            lines.append(f"{pad}{{")
            for sub in arm_body:
                lines.extend(compile_instruction_csharp(sub, indent + 1, ctx))
            lines.append(f"{pad}}}")

    return lines


def compile_branch_csharp(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    """Compile BRANCH instruction to C# if/else."""
    pad = "    " * indent
    lines = []
    condition = inst.get('condition', '')
    cases = inst.get('cases', {})

    if isinstance(condition, dict):
        # Phase 3b: dotted identifiers on JsonNode roots become indexer access
        # (root["k"]!.GetValue<T>()). Type hint inferred from the scalar operand.
        left_raw = condition.get('left', '')
        right_raw = condition.get('right', '')
        hint_left = _scalar_type_hint(right_raw)
        hint_right = _scalar_type_hint(left_raw)
        # When comparing a JsonNode path against a bare atom with no explicit type,
        # default to string (the most common case for MATCH-like dispatches).
        def _is_json_root(v):
            if isinstance(v, str) and '.' in v:
                root = v.split('.')[0]
                return (ctx.get('locals') or {}).get(root) in ('JsonNode', 'any_json')
            return False
        op = condition.get('op', '==')
        numeric_ops = {'<', '>', '<=', '>='}
        default_hint = 'int' if op in numeric_ops else 'string'
        if hint_left is None and _is_json_root(left_raw) and isinstance(right_raw, str):
            hint_left = default_hint
        if hint_right is None and _is_json_root(right_raw) and isinstance(left_raw, str):
            hint_right = default_hint
        left = _cs_expr(left_raw, ctx, hint=hint_left)
        right = _cs_expr(right_raw, ctx, hint=hint_right)
        if isinstance(right_raw, str) and right_raw not in ((ctx.get('locals') or {})) and not right_raw.startswith('"') and '.' not in right_raw:
            try: float(right_raw)
            except ValueError:
                if right_raw.lower() not in ('true', 'false', 'null', 'nil'):
                    right = f'"{right_raw}"'
        if op == 'NOT_IN':
            cond_str = f"!{right}.Contains({left})"
        elif op == 'IN':
            cond_str = f"{right}.Contains({left})"
        else:
            cond_str = f"{left} {op} {right}"
    else:
        cond_str = str(condition)

    lines.append(f"{pad}if ({cond_str})")
    lines.append(f"{pad}{{")
    for sub in cases.get('TRUE', []):
        lines.extend(compile_instruction_csharp(sub, indent + 1, ctx))
    lines.append(f"{pad}}}")

    if cases.get('FALSE'):
        lines.append(f"{pad}else")
        lines.append(f"{pad}{{")
        for sub in cases['FALSE']:
            lines.extend(compile_instruction_csharp(sub, indent + 1, ctx))
        lines.append(f"{pad}}}")

    return lines


# ============================================================================
# CODE GENERATORS
# ============================================================================

def pascal_case(s: str) -> str:
    return ''.join(w.capitalize() for w in s.split('_'))


def camel_case(s: str) -> str:
    parts = s.split('_')
    return parts[0] + ''.join(w.capitalize() for w in parts[1:])


def _aot_json_types(bc) -> list[str]:
    """Types the source-gen JsonSerializerContext must cover for NativeAOT.

    Schemas drive the list (each schema → T, TDto, List<T>); the native error
    model is always included so RESPOND/RESPOND_STATUS paths work without
    reflection.
    """
    schema_names = sorted(bc.schemas.keys())
    types: list[str] = []
    for s in schema_names:
        types.append(s)
        # Match the `if dto_fields:` guard in generate_schema_csharp — a
        # schema whose fields are all auto_gen does not get a {Schema}Dto
        # record emitted, so listing it here would reference a missing type.
        schema_def = bc.schemas.get(s) or {}
        fields = schema_def.get('fields', {}) or {}
        if any(not f.get('auto_gen') for f in fields.values()):
            types.append(f"{s}Dto")
        types.append(f"List<{s}>")
    types += ["AiLangError", "ErrorDetail", "TokenResponse"]
    return types


# ── C# Generator ──

def _resolve_env_var(bc, config_key: str, fallback: str) -> str:
    """Given a config key like 'jwt_secret', return the env var name from (config ...) or fallback."""
    config = bc.config or {}
    entry = config.get(config_key)
    if isinstance(entry, dict):
        if entry.get('type') == 'secret':
            return entry.get('ref') or fallback
    return fallback


def _build_route_params(path: str, handler_def: dict) -> tuple:
    """Walk `:name` tokens in path, return (cs_params, rewritten_path, typed_params).

    typed_params: list of dicts {name, route_var, ailang_type}
    """
    import re
    handler_params = handler_def.get('params', {}) or {}
    pattern = re.compile(r':(\w+)')
    cs_params = []
    typed = []
    new_path = path
    for name in pattern.findall(path):
        ailang_type = handler_params.get(name, 'string')
        # For parseable types, use a __raw suffix so the READ_PARAM op can bind
        # the clean name as the typed variable without shadowing.
        if ailang_type in ('uuid', 'int'):
            route_var = f"{name}__raw"
        else:
            route_var = name
        new_path = new_path.replace(f':{name}', '{' + route_var + '}', 1)
        cs_params.append(f"string {route_var}")
        typed.append({'name': name, 'route_var': route_var, 'type': ailang_type})
    return cs_params, new_path, typed


def generate_csharp(bc, output_dir: str, aot: bool = False):
    """Generate a complete C# Minimal API project from bytecode."""
    os.makedirs(output_dir, exist_ok=True)

    service_name = bc.service['name']
    port = bc.service['port']

    # Detect PostgreSQL-backed stores — changes DI, async, and the Npgsql bootstrap.
    pg_stores = {
        n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'
    }
    is_pg = bool(pg_stores)

    # Resolve env vars from config (jwt_secret → JWT_SECRET, db_connection → DB_CONNECTION_STRING)
    jwt_env = _resolve_env_var(bc, bc.auth.get('secret_ref', 'jwt_secret') if bc.auth else 'jwt_secret', 'JWT_SECRET')
    db_conn_env = _resolve_env_var(bc, 'db_connection', 'DB_CONNECTION_STRING')

    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("// AILang Transpiler Output — C# (.NET 10 LTS)")
    lines.append(f"// Source: {service_name} (AILang Bytecode v{bc.raw.get('version', '0.8')})")
    lines.append("// Auto-generated by AILang Transpiler")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("")
    lines.append("using System.Collections.Concurrent;")
    lines.append("using System.Text.Json;")
    lines.append("using System.Text.Json.Serialization;")
    # Polly extension methods (.AddCircuitBreaker, .AddRetry, .AddTimeout) live
    # in the root Polly namespace.
    uses_resilience_cs = any(
        svc.get('resilience') for svc in (bc.dependencies or {}).values()
        if svc.get('type') == 'SERVICE_REF'
    )
    if uses_resilience_cs:
        lines.append("using Polly;")
    lines.append("")
    lines.append("var builder = WebApplication.CreateBuilder(args);")
    lines.append("")

    # Auth setup
    if bc.auth:
        lines.append("// ── AUTH (from bytecode) ──")
        lines.append('builder.Services.AddAuthentication("Bearer")')
        lines.append('    .AddJwtBearer("Bearer", options =>')
        lines.append('    {')
        lines.append(f'        var secret = Environment.GetEnvironmentVariable("{jwt_env}") ?? "dev-secret-key-min-32-characters!!";')
        lines.append('        options.TokenValidationParameters = new Microsoft.IdentityModel.Tokens.TokenValidationParameters')
        lines.append('        {')
        lines.append('            ValidateIssuer = false, ValidateAudience = false, ValidateLifetime = false,')
        lines.append('            IssuerSigningKey = new Microsoft.IdentityModel.Tokens.SymmetricSecurityKey(')
        lines.append('                System.Text.Encoding.UTF8.GetBytes(secret))')
        lines.append('        };')
        lines.append('    });')
        lines.append('builder.Services.AddAuthorization();')
        lines.append('')

    # AOT: wire the source-generated JsonSerializerContext into Http JSON options
    # so Minimal API request/response (de)serialization uses the metadata graph
    # emitted by [JsonSerializable(...)] instead of runtime reflection.
    if aot:
        service_pascal = pascal_case(service_name)
        lines.append('// ── AOT: register source-gen JSON context ──')
        lines.append('builder.Services.ConfigureHttpJsonOptions(o =>')
        lines.append(f'    o.SerializerOptions.TypeInfoResolverChain.Insert(0, {service_pascal}JsonContext.Default));')
        lines.append('')

    # PostgreSQL datasource + DI
    if is_pg:
        lines.append("// ── POSTGRES DATASOURCE (from bytecode store pool) ──")
        lines.append(f'var _pgConnStr = Environment.GetEnvironmentVariable("{db_conn_env}")')
        lines.append('    ?? "Host=localhost;Port=5432;Database=ailang;Username=postgres;Password=postgres";')
        lines.append('var _pgDsBuilder = new Npgsql.NpgsqlDataSourceBuilder(_pgConnStr);')
        lines.append('var _pgDataSource = _pgDsBuilder.Build();')
        lines.append('builder.Services.AddSingleton(_pgDataSource);')
        for store_name, store_def in pg_stores.items():
            schema = store_def['schema']
            lines.append(f"// ── DI: {store_name} → Postgres{schema}Store ──")
            lines.append(f"builder.Services.AddSingleton<I{schema}Store>(sp => new Postgres{schema}Store(sp.GetRequiredService<Npgsql.NpgsqlDataSource>()));")
        lines.append("")

    # In-memory stores (if any)
    mem_stores = {n: s for n, s in bc.stores.items() if s.get('provider') != 'PostgreSQL'}
    for store_name, store_def in mem_stores.items():
        schema = store_def['schema']
        lines.append(f"// ── DI: {store_name} → InMemory{schema}Store ──")
        lines.append(f"builder.Services.AddSingleton<I{schema}Store, InMemory{schema}Store>();")
    if mem_stores:
        lines.append("")

    # HttpClient per SERVICE_REF dependency — base address from env (with default fallback).
    # When a resilience policy is attached, emit two named clients per service:
    #   - "{svc}"          : full pipeline (CB + retry + timeout) for idempotent calls
    #   - "{svc}_no_retry" : pipeline without retry for non-idempotent calls
    service_refs = {n: d for n, d in (bc.dependencies or {}).items() if d.get('type') == 'SERVICE_REF'}
    policies = (bc.resilience or {}).get('policies', {}) or {}

    # Register one CircuitBreakerManualControl per policy so tests (and any
    # admin tooling) can `.IsolateAsync()` to force a circuit open. Keyed by
    # policy name so the generated pipeline can resolve its own control.
    for policy_name, policy in policies.items():
        if policy.get('circuit_breaker'):
            lines.append(f'builder.Services.AddKeyedSingleton<Polly.CircuitBreaker.CircuitBreakerManualControl>("{policy_name}", (_, _) => new Polly.CircuitBreaker.CircuitBreakerManualControl());')
    if any(p.get('circuit_breaker') for p in policies.values()):
        lines.append("")

    def _emit_http_client(svc_name, svc_def, policy, include_retry):
        env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
        default_url = svc_def.get('default', 'http://localhost')
        client_name = svc_name if include_retry else f"{svc_name}_no_retry"
        lines.append(f"// ── DI: HttpClient '{client_name}' {'(with retry)' if include_retry else '(no retry — non-idempotent)'} ──")
        lines.append(f'builder.Services.AddHttpClient("{client_name}", c =>')
        lines.append("{")
        lines.append(f'    var baseUrl = Environment.GetEnvironmentVariable("{env_var}") ?? "{default_url}";')
        lines.append("    c.BaseAddress = new Uri(baseUrl);")
        lines.append("})")
        if policy:
            lines.extend(generate_resilience_handler_csharp(policy, include_retry=include_retry, indent=1))
        # Terminator — last statement in the fluent chain gets ';'.
        lines[-1] = lines[-1] + ";"

    for svc_name, svc_def in service_refs.items():
        policy_name = svc_def.get('resilience')
        policy = policies.get(policy_name) if policy_name else None
        # Primary client (with retry when policy defines it).
        _emit_http_client(svc_name, svc_def, policy, include_retry=True)
        # Only emit the no-retry sibling when the policy actually defines retry —
        # otherwise it's identical to the primary and would just bloat DI.
        if policy and policy.get('retry'):
            _emit_http_client(svc_name, svc_def, policy, include_retry=False)
    if service_refs:
        lines.append("")

    # Event bus (singleton) when any EMIT_EVENT or EVENT_HANDLER exists.
    # EMIT_EVENT can be nested inside MATCH/BRANCH bodies — walk recursively.
    def _walk_all_ops_in_handler(handler):
        def _walk(insts):
            for inst in insts:
                yield inst.get('op')
                for case_insts in (inst.get('cases') or {}).values():
                    if isinstance(case_insts, list):
                        yield from _walk(case_insts)
                for sub in inst.get('body', []) or []:
                    if isinstance(sub, dict):
                        yield from _walk([sub])
                for fiber in inst.get('fibers', []) or []:
                    yield from _walk(fiber.get('instructions', []) or [])
        yield from _walk(handler.get('instructions', []))

    event_handlers_cs = [
        (n, h) for n, h in bc.handlers.items() if h.get('type') == 'EVENT_HANDLER'
    ]
    uses_events_cs = bool(event_handlers_cs) or any(
        op == 'EMIT_EVENT'
        for h in bc.handlers.values()
        for op in _walk_all_ops_in_handler(h)
    )
    if uses_events_cs:
        lines.append("// ── DI: in-process event bus ──")
        lines.append("builder.Services.AddSingleton<IEventBus, InMemoryEventBus>();")
        lines.append("")

    # IAiLangClock — injected when handlers call GET_CURRENT from=clock. Tests
    # can swap in FakeAiLangClock via (override_time …) to fix DayOfWeek/UtcNow.
    def _handler_uses_clock(h):
        def _w(insts):
            for i in insts:
                if not isinstance(i, dict):
                    continue
                if i.get('op') == 'GET_CURRENT' and i.get('from') == 'clock':
                    return True
                if isinstance(i.get('body'), list) and _w(i['body']):
                    return True
                if isinstance(i.get('cases'), dict):
                    for v in i['cases'].values():
                        if isinstance(v, list) and _w(v):
                            return True
            return False
        return _w(h.get('instructions', []))
    uses_clock_cs = any(_handler_uses_clock(h) for h in bc.handlers.values())
    if uses_clock_cs:
        lines.append("// ── DI: IAiLangClock (swap with FakeAiLangClock in tests) ──")
        lines.append("builder.Services.AddSingleton<IAiLangClock, SystemAiLangClock>();")
        lines.append("")

    # CORS
    if bc.cors:
        origins = bc.cors.get('origins', ['*'])
        methods = bc.cors.get('methods', [])
        headers = bc.cors.get('headers', [])
        max_age = bc.cors.get('max_age', 3600)
        lines.append("// ── CORS (from bytecode) ──")
        lines.append("builder.Services.AddCors(o => o.AddDefaultPolicy(p =>")
        if '*' in origins:
            lines.append("    p.AllowAnyOrigin()")
        else:
            origins_str = ", ".join(['"' + o + '"' for o in origins])
            lines.append('    p.WithOrigins(' + origins_str + ')')
        methods_str = ", ".join(['"' + m + '"' for m in methods])
        headers_str = ", ".join(['"' + h + '"' for h in headers])
        lines.append('     .WithMethods(' + methods_str + ')')
        lines.append('     .WithHeaders(' + headers_str + ')')
        lines.append(f'     .SetPreflightMaxAge(TimeSpan.FromSeconds({max_age}))));')
        lines.append("")

    lines.append("var app = builder.Build();")
    lines.append("app.UseCors();")
    if bc.auth:
        lines.append("app.UseAuthentication();")
        lines.append("app.UseAuthorization();")
    lines.append("")

    # Tracing middleware
    lines.append("// ── TRACING: native observability ──")
    lines.append("app.Use(async (context, next) =>")
    lines.append("{")
    lines.append('    var traceId = Guid.NewGuid().ToString("N")[..16];')
    lines.append('    context.Items["trace_id"] = traceId;')
    lines.append('    context.Response.Headers["X-Trace-Id"] = traceId;')
    lines.append('    var sw = System.Diagnostics.Stopwatch.StartNew();')
    lines.append('    Console.WriteLine($"[TRACE] {traceId} | {context.Request.Method} {context.Request.Path} | START");')
    lines.append("    await next();")
    lines.append('    sw.Stop();')
    lines.append('    Console.WriteLine($"[TRACE] {traceId} | {context.Request.Method} {context.Request.Path} | {context.Response.StatusCode} | {sw.ElapsedMilliseconds}ms");')
    lines.append("});")
    lines.append("")

    # ReadRow<T> helpers for SQL_QUERY / SQL_STREAM (must precede the lambdas)
    row_mappers = generate_row_mappers_csharp(bc)
    if row_mappers:
        lines.extend(row_mappers)

    # Base path from common prefix
    paths = [r['path'] for r in bc.routes]
    # Common PATH-SEGMENT prefix across all routes (not char-wise — that would
    # eat partial words, e.g. /check + /cancel would yield "/c").
    def _segment_common_prefix(all_paths):
        if not all_paths:
            return ''
        split = [p.strip('/').split('/') for p in all_paths]
        common = []
        for i in range(min(len(parts) for parts in split)):
            seg = split[0][i]
            if seg.startswith(':') or '{' in seg:
                break
            if all(parts[i] == seg for parts in split):
                common.append(seg)
            else:
                break
        return ('/' + '/'.join(common)) if common else ''

    base_path = _segment_common_prefix(paths)
    if not base_path:
        base_path = '/' + service_name.replace('_service', 's')

    # Wire up EVENT_HANDLER subscriptions before routing — so any request that
    # emits an event right after `app.Run` already has listeners in place.
    if event_handlers_cs:
        lines.append("// ── Event subscriptions (startup) ──")
        lines.append("var _bus = app.Services.GetRequiredService<IEventBus>();")
        for h_name, h_def in event_handlers_cs:
            evt = h_def.get('event') or h_name
            method_name = pascal_case(h_name) if h_name.startswith('on_') else 'On' + pascal_case(h_name)
            lines.append(f'_bus.Subscribe("{evt}", {method_name});')
        lines.append("")

    lines.append(f'var api = app.MapGroup("{base_path}");')
    lines.append("")

    # Primary store (first store is the dominant one for handler codegen)
    primary_store_name, primary_store_def = next(iter(bc.stores.items())) if bc.stores else (None, None)
    primary_schema = primary_store_def['schema'] if primary_store_def else None

    for route in bc.routes:
        handler_name = route['handler']
        handler_def = bc.handlers[handler_name]
        # EVENT_HANDLERs never map to an HTTP route — they're dispatched by the bus.
        if handler_def.get('type') == 'EVENT_HANDLER':
            continue
        method = route['method']
        path = route['path'].replace(base_path, '') or '/'
        auth = handler_def.get('auth')
        instructions = handler_def.get('instructions', [])

        def _walk_ops(inst_list):
            for i in inst_list or []:
                if not isinstance(i, dict):
                    continue
                yield i
                cases = i.get('cases') or {}
                for case_insts in cases.values():
                    if isinstance(case_insts, list):
                        yield from _walk_ops(case_insts)
                # ITERATE/ITERATE_PARALLEL body + PARALLEL fibers
                for sub in i.get('body', []) or []:
                    if isinstance(sub, dict):
                        yield from _walk_ops([sub])
                for fiber in i.get('fibers', []) or []:
                    yield from _walk_ops(fiber.get('instructions', []) or [])

        all_ops = [i.get('op') for i in _walk_ops(instructions)]
        needs_ctx = (
            auth != 'PUBLIC'
            or any(op in ('READ_QUERY', 'READ_BODY', 'STREAM_RESPOND') for op in all_ops)
        )
        needs_body = 'READ_BODY' in all_ops

        # Route parameters (typed rewriting: `:id` → `{id__raw}` for uuid/int)
        cs_params, new_path, _typed = _build_route_params(path, handler_def)
        path = new_path

        lambda_params = list(cs_params)
        if needs_ctx:
            lambda_params.append("HttpContext ctx")
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            camel = camel_case(store_name)
            lambda_params.append(f"I{schema}Store {camel}")

        uses_call_service = 'CALL_SERVICE' in all_ops
        if uses_call_service:
            lambda_params.append("IHttpClientFactory httpClientFactory")
        uses_emit_event = 'EMIT_EVENT' in all_ops
        if uses_emit_event:
            lambda_params.append("IEventBus eventBus")
        # GET_CURRENT clock.* → IAiLangClock so tests can substitute a fake
        # via (override_time …). Injected only when the handler references it.
        def _walk_for_clock(insts):
            for i in insts:
                if not isinstance(i, dict):
                    continue
                if i.get('op') == 'GET_CURRENT' and i.get('from') == 'clock':
                    return True
                for k in ('body',):
                    if isinstance(i.get(k), list) and _walk_for_clock(i[k]):
                        return True
                if isinstance(i.get('cases'), dict):
                    for v in i['cases'].values():
                        if isinstance(v, list) and _walk_for_clock(v):
                            return True
            return False
        uses_clock = _walk_for_clock(instructions)
        if uses_clock:
            lambda_params.append("IAiLangClock clock")

        param_str = ", ".join(lambda_params)
        is_async = is_pg or needs_body or uses_call_service or any(
            op in ('SQL_QUERY', 'SQL_STREAM', 'SQL_EXEC', 'STORE_FIND_BY', 'STORE_FILTER',
                   'STREAM_RESPOND', 'SAGA_COMPENSATE', 'PARALLEL', 'AWAIT_ALL')
            for op in all_ops
        )

        lines.append(f'// ROUTE {method} "{base_path}{path}" → {handler_name} ({auth if auth == "PUBLIC" else "REQUIRES auth"})')
        if is_async:
            lines.append(f'api.Map{method.capitalize()}("{path}", async ({param_str}) =>')
        else:
            lines.append(f'api.Map{method.capitalize()}("{path}", ({param_str}) =>')
        lines.append("{")

        if auth != 'PUBLIC':
            lines.append("    if (!(ctx.User.Identity?.IsAuthenticated ?? false))")
            if aot:
                lines.append(f"        return Results.Json(AiLangError.Unauthorized(), {pascal_case(service_name)}JsonContext.Default.AiLangError, statusCode: 401);")
            else:
                lines.append("        return Results.Json(AiLangError.Unauthorized(), statusCode: 401);")
            lines.append("")

        ctx = {
            'is_pg': is_pg,
            'store_var': camel_case(primary_store_name) if primary_store_name else 'store',
            'store_def': primary_store_def,
            'returns': handler_def.get('returns'),
            'custom_errors': bc.custom_errors,
            'service_refs': service_refs,
            'resilience_policies': policies,
            'schemas': bc.schemas,  # For Phase 3b — CONSTRUCT uses field types as JsonNode-cast hints.
            # AOT: handler-level emission switches reflection-based JSON sites
            # (ReadFromJsonAsync<T>, Results.Json<T>) to the source-gen context
            # overload when 'aot' is True. service_name supplies the context
            # class prefix (<Service>JsonContext.Default.<Type>).
            'aot': aot,
            'service_name': service_name,
        }
        for inst in instructions:
            lines.extend(compile_instruction_csharp(inst, 1, ctx))

        # Safety return — always emit so complex MATCH/BRANCH trees that the
        # compiler can't prove as exhaustive don't trip CS1643. Wrap in the
        # CS0162 pragma so handlers that *do* prove exhaustive don't warn
        # about the unreachable fallback.
        lines.append("#pragma warning disable CS0162")
        lines.append("    return Results.StatusCode(500); // unreachable — safety fallback")
        lines.append("#pragma warning restore CS0162")
        lines.append("});")
        lines.append("")

    # Token endpoint for testing
    lines.append("// ── Token generator (for testing) ──")
    lines.append('app.MapPost("/auth/token", () =>')
    lines.append('{')
    lines.append(f'    var secret = Environment.GetEnvironmentVariable("{jwt_env}") ?? "dev-secret-key-min-32-characters!!";')
    lines.append('    var key = new Microsoft.IdentityModel.Tokens.SymmetricSecurityKey(System.Text.Encoding.UTF8.GetBytes(secret));')
    lines.append('    var creds = new Microsoft.IdentityModel.Tokens.SigningCredentials(key, Microsoft.IdentityModel.Tokens.SecurityAlgorithms.HmacSha256);')
    lines.append('    var token = new System.IdentityModel.Tokens.Jwt.JwtSecurityToken(')
    lines.append('        claims: [new System.Security.Claims.Claim("sub", "test-user")],')
    lines.append('        expires: DateTime.UtcNow.AddHours(1), signingCredentials: creds);')
    lines.append('    return Results.Ok(new TokenResponse(new System.IdentityModel.Tokens.Jwt.JwtSecurityTokenHandler().WriteToken(token)));')
    lines.append('});')
    lines.append("")

    lines.append(f'Console.WriteLine("[SERVICE] {service_name} STARTED on :{port}");')
    lines.append('Console.WriteLine("[READY]");')

    # Inter-service HTTP helper is a static local function inside the top-level
    # statements — it MUST be emitted before any type declarations (CS8803)
    # and before `app.Run` so the static local is in scope for lambdas above.
    uses_call_service_anywhere = any(
        inst.get('op') == 'CALL_SERVICE'
        for h in bc.handlers.values()
        for inst in h.get('instructions', [])
    ) or bool(service_refs)
    if uses_call_service_anywhere:
        lines.append("")
        lines.extend(generate_service_client_csharp())
        lines.append("")

    # EVENT_HANDLER static local functions — emitted before app.Run so the
    # Subscribe() calls above resolve the method group reference.
    for h_name, h_def in event_handlers_cs:
        lines.append("")
        lines.extend(_emit_event_handler_csharp(h_name, h_def, bc))

    # Saga compensators — static local funcs, referenced by SAGA_COMPENSATE.
    if bc.sagas:
        lines.append("")
        lines.extend(generate_saga_compensators_csharp(bc))

    lines.append(f'app.Run("http://0.0.0.0:{port}");')
    lines.append("")
    # Test seam: WebApplicationFactory<Program> from Microsoft.AspNetCore.Mvc.Testing
    # requires a reachable Program type — the top-level Program class is internal
    # by default, so we expose it via a partial declaration.
    lines.append("public partial class Program { }")
    lines.append("")

    # Dev-token endpoint response — concrete record (not anonymous) so it can be
    # added to the source-gen JsonSerializerContext under --aot. Always emitted
    # to keep AOT and non-AOT output identical; wire format {"token":"..."} is
    # unchanged from the previous anonymous-type version.
    lines.append('public record TokenResponse([property: System.Text.Json.Serialization.JsonPropertyName("token")] string Token);')
    lines.append("")

    # Schemas
    for schema_name, schema_def in bc.schemas.items():
        lines.extend(generate_schema_csharp(schema_name, schema_def))
        lines.append("")

    # Paginated<T> (shared)
    if is_pg or any(
        i.get('op') in ('STORE_LIST_ALL', 'STORE_FILTER') and i.get('paginate')
        for h in bc.handlers.values()
        for i in h.get('instructions', [])
    ):
        lines.extend(generate_paginated_csharp())
        lines.append("")

    # Stores (per provider)
    for store_name, store_def in bc.stores.items():
        schema = store_def['schema']
        schema_def = bc.schemas[schema]
        if store_def.get('provider') == 'PostgreSQL':
            lines.extend(generate_postgres_store_csharp(store_name, store_def, schema_def))
        else:
            lines.extend(generate_store_csharp(schema, schema_def, store_def))
        lines.append("")

    # Error model
    lines.extend(generate_error_model_csharp(bc.custom_errors))

    # Event bus types (emitted AFTER app.Run since they are type declarations).
    if uses_events_cs:
        lines.append("")
        lines.extend(generate_event_bus_csharp())
    if uses_clock_cs:
        lines.append("")
        lines.extend(generate_clock_csharp())

    # AOT: emit the source-generated JsonSerializerContext at the END of the file,
    # AFTER every record/class it references (typeof(Product/ProductDto/...) must
    # resolve). The class is partial so the source generator can fill in the
    # per-type metadata at build time.
    if aot:
        service_pascal = pascal_case(service_name)
        json_types = _aot_json_types(bc)
        lines.append("")
        lines.append("// ── AOT: source-generated JSON context ──")
        lines.append("[System.Text.Json.Serialization.JsonSourceGenerationOptions(WriteIndented = false)]")
        for t in json_types:
            lines.append(f"[System.Text.Json.Serialization.JsonSerializable(typeof({t}))]")
        lines.append(f"internal partial class {service_pascal}JsonContext : System.Text.Json.Serialization.JsonSerializerContext;")

    with open(os.path.join(output_dir, "Program.cs"), 'w') as f:
        f.write('\n'.join(lines))

    # .csproj — include Npgsql when a PG store is present; resilience package
    # when any service_ref has a policy attached.
    pkg_refs = [
        '    <PackageReference Include="Microsoft.AspNetCore.Authentication.JwtBearer" Version="10.0.0" />',
        '    <PackageReference Include="System.IdentityModel.Tokens.Jwt" Version="8.2.1" />',
    ]
    if is_pg:
        pkg_refs.append('    <PackageReference Include="Npgsql" Version="10.0.0" />')
    needs_resilience = any(
        svc.get('resilience') for svc in (bc.dependencies or {}).values()
        if svc.get('type') == 'SERVICE_REF'
    )
    if needs_resilience:
        pkg_refs.append('    <PackageReference Include="Microsoft.Extensions.Http.Resilience" Version="9.0.0" />')

    aot_props = (
        '    <PublishAot>true</PublishAot>\n'
        '    <InvariantGlobalization>true</InvariantGlobalization>\n'
        '    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>\n'
        '    <TrimmerSingleWarn>false</TrimmerSingleWarn>\n'
    ) if aot else ''
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        + aot_props +
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <!-- Tests/ is its own project; ASP.NET would otherwise auto-include it. -->\n'
        '    <Compile Remove="Tests/**" />\n'
        '    <Content Remove="Tests/**" />\n'
        '    <None Remove="Tests/**" />\n'
        '  </ItemGroup>\n'
        '  <ItemGroup>\n'
        + '\n'.join(pkg_refs) + '\n'
        '  </ItemGroup>\n'
        '</Project>'
    )
    with open(os.path.join(output_dir, f"{pascal_case(service_name)}.csproj"), 'w') as f:
        f.write(csproj)

    print(f"  ✓ C# project generated in {output_dir}/")


def generate_schema_csharp(name: str, schema_def: dict) -> list[str]:
    """Generate C# record from AILang schema."""
    lines = []
    fields = schema_def.get('fields', {})

    # Main record
    lines.append(f"// SCHEMA: {name} (from bytecode)")
    lines.append(f"public record {name}")
    lines.append("{")
    for field_name, field_def in fields.items():
        ftype = field_def['type']
        list_of = field_def.get('of')
        csharp_type = TypeMapper.to_csharp(ftype, list_of)
        prop_name = pascal_case(field_name)
        auto_gen = field_def.get('auto_gen', False)
        required = field_def.get('required', False)
        # Auto-gen (id/created_at/...) and required fields are always set by the
        # system — mark them `required` so the compiler enforces initialization
        # in new-expressions without needing null-suppression or default values.
        is_mandatory = auto_gen or required
        if is_mandatory:
            modifier = "required "
            nullable = ""
            default = ""
        else:
            modifier = ""
            nullable = "" if ftype == 'list' else "?"
            default = " = [];" if ftype == 'list' else ""
        lines.append(f'    [JsonPropertyName("{field_name}")]')
        lines.append(f"    public {modifier}{csharp_type}{nullable} {prop_name} {{ get; init; }}{default}")
    lines.append("}")

    # DTO for create/update (without auto_gen fields)
    dto_fields = {k: v for k, v in fields.items() if not v.get('auto_gen')}
    if dto_fields:
        lines.append("")
        lines.append(f"public record {name}Dto")
        lines.append("{")
        for field_name, field_def in dto_fields.items():
            ftype = field_def['type']
            list_of = field_def.get('of')
            csharp_type = TypeMapper.to_csharp(ftype, list_of)
            required = field_def.get('required', False)
            prop_name = pascal_case(field_name)
            if required:
                modifier = "required "
                nullable = ""
            else:
                modifier = ""
                nullable = "?" if ftype in ('string', 'list', 'object') else ""
            lines.append(f'    [JsonPropertyName("{field_name}")]')
            lines.append(f"    public {modifier}{csharp_type}{nullable} {prop_name} {{ get; init; }}")

        # Validate() — mirrors Go's (d XDto) Validate() *AiLangError. Returns null
        # on success; ensures the READ_BODY contract ("run all schema validations")
        # holds regardless of target language.
        lines.append("")
        lines.append("    public AiLangError? Validate()")
        lines.append("    {")
        for field_name, field_def in dto_fields.items():
            ftype = field_def.get('type')
            required = field_def.get('required', False)
            prop = pascal_case(field_name)
            if ftype == 'string' and required:
                min_len = field_def.get('min', 0)
                max_len = field_def.get('max', 0)
                if min_len > 0:
                    lines.append(f'        if (string.IsNullOrWhiteSpace({prop}) || {prop}!.Length < {min_len})')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "required, min length {min_len}");')
                else:
                    lines.append(f'        if ({prop} == null)')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "required");')
                if max_len > 0:
                    lines.append(f'        if ({prop}!.Length > {max_len})')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "max length {max_len}");')
            if ftype in ('int', 'decimal'):
                gt = field_def.get('gt')
                gte = field_def.get('gte')
                suffix = 'M' if ftype == 'decimal' else ''
                if gt is not None:
                    lines.append(f'        if ({prop} <= {gt}{suffix})')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "must be > {gt}");')
                if gte is not None:
                    lines.append(f'        if ({prop} < {gte}{suffix})')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "must be >= {gte}");')
            if ftype == 'list':
                min_items = field_def.get('min_items')
                if min_items and required:
                    lines.append(f'        if ({prop} == null || {prop}.Count < {min_items})')
                    lines.append(f'            return AiLangError.Validation("{field_name}", "required, min {min_items} items");')
        lines.append("        return null;")
        lines.append("    }")
        lines.append("}")

    return lines


def generate_saga_compensators_csharp(bc) -> list[str]:
    """Emit a static local function per saga that logs the compensation plan.

    Full saga orchestration (execute compensations in reverse order for steps
    that actually completed) requires explicit step tracking in the bytecode —
    scheduled for Phase 6b. For now the helper logs the plan so operators can
    see what WOULD be compensated and intervene manually if needed.
    """
    lines = []
    for saga_name, saga_def in (bc.sagas or {}).items():
        fn = 'Compensate' + pascal_case(saga_name) + 'Async'
        lines.append(f"// ── SAGA COMPENSATOR: {saga_name} ──")
        lines.append(f"static async Task {fn}(ILogger? logger = null)")
        lines.append("{")
        lines.append(f'    Console.WriteLine("[SAGA] compensation triggered for \'{saga_name}\' — rolling back in reverse order");')
        for step in reversed(saga_def.get('steps', [])):
            step_name = step['name']
            comp = step.get('compensate', {})
            op = comp.get('op', 'LOG')
            plan = _describe_compensate(op, comp)
            lines.append(f'    Console.WriteLine("[SAGA]   ← {step_name}: {plan}");')
        lines.append("    await Task.CompletedTask;")
        lines.append("}")
        lines.append("")
    return lines


def _describe_compensate(op: str, comp: dict) -> str:
    """One-line human description of a compensate step for the log plan."""
    if op == 'CALL_SERVICE':
        svc = comp.get('service', '?')
        method = comp.get('method', '?')
        path = comp.get('path', '?')
        return f"CALL_SERVICE {svc} {method} {path}".replace('"', '')
    if op == 'STORE_UPDATE_FIELDS':
        store = comp.get('store', '?')
        return f"STORE_UPDATE_FIELDS {store}".replace('"', '')
    if op == 'LOG':
        msg = str(comp.get('message', '')).replace('"', "'")
        return f"LOG {msg}"
    return op


def _emit_event_handler_csharp(handler_name: str, handler_def: dict, bc) -> list[str]:
    """Emit an EVENT_HANDLER as a `static async Task` local function.

    Signature matches `Func<object?, IServiceProvider, Task>` so the bus can
    invoke it directly. `eventData` is the object published; services resolved
    via `sp.GetRequiredService<>()` when needed.
    """
    method_name = pascal_case(handler_name) if handler_name.startswith('on_') else 'On' + pascal_case(handler_name)
    lines = [
        f"static async Task {method_name}(object? eventData, IServiceProvider sp)",
        "{",
    ]
    ctx = {
        'is_pg': False,
        'store_def': None,
        'custom_errors': bc.custom_errors,
        'service_refs': {},
        'resilience_policies': {},
        'schemas': bc.schemas if bc else {},
        'locals': {},
    }
    # Resolve DI services the handler body may need (CALL_SERVICE → IHttpClientFactory,
    # STORE ops → IXStore). EVENT_HANDLERs are `static` local functions — they can't
    # capture outer-scope locals, so we resolve from `sp` explicitly.
    def _walk_ops(insts):
        ops = set()
        for i in insts:
            if isinstance(i, dict):
                ops.add(i.get('op', ''))
                for key in ('body',):
                    if isinstance(i.get(key), list):
                        ops |= _walk_ops(i[key])
                if isinstance(i.get('cases'), dict):
                    for v in i['cases'].values():
                        if isinstance(v, list):
                            ops |= _walk_ops(v)
        return ops
    all_ops = _walk_ops(handler_def.get('instructions', []))
    if 'CALL_SERVICE' in all_ops:
        lines.append('    var httpClientFactory = sp.GetRequiredService<IHttpClientFactory>();')
    # Stores: resolve the primary store if any STORE_ op appears.
    if any(op.startswith('STORE_') for op in all_ops) and bc.stores:
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            lines.append(f'    var {camel_case(store_name)} = sp.GetRequiredService<I{schema}Store>();')
            ctx['store_var'] = camel_case(store_name)
            ctx['store_def'] = store_def

    # Cast eventData to JsonNode so `event.data.X` paths resolve via Phase 3b
    # indexer access. AILang's `event.data` IS the full payload — the `.data`
    # segment is stripped by registering the root as `event.data` → `_evtData`.
    # NOTE (NativeAOT): JsonSerializer.Serialize(eventData) where eventData is
    # typed `object?` is reflection-based — a service using EVENT_HANDLER under
    # --aot will trip IL2026/IL3050 at publish time. Fix when needed: dispatch
    # on runtime type via the JsonContext's TypeInfoResolverChain instead of
    # round-tripping object→string→JsonNode.
    lines.append('    var _evtData = System.Text.Json.Nodes.JsonNode.Parse(')
    lines.append('        System.Text.Json.JsonSerializer.Serialize(eventData));')
    ctx['locals']['_evtData'] = 'JsonNode'

    # Rewrite event.data.X references in instruction trees to _evtData.X
    import copy
    def _rewrite_event_refs(inst):
        inst = copy.deepcopy(inst)
        for key in ('collection', 'value', 'assign', 'name', 'target'):
            v = inst.get(key)
            if isinstance(v, str) and v.startswith('event.data.'):
                inst[key] = '_evtData.' + v[len('event.data.'):]
            elif isinstance(v, str) and v == 'event.data':
                inst[key] = '_evtData'
        # Recurse into body/cases
        if 'body' in inst and isinstance(inst['body'], list):
            inst['body'] = [_rewrite_event_refs(i) for i in inst['body'] if isinstance(i, dict)]
        if 'cases' in inst and isinstance(inst['cases'], dict):
            inst['cases'] = {k: [_rewrite_event_refs(i) for i in v if isinstance(i, dict)]
                             for k, v in inst['cases'].items()}
        return inst

    for inst in handler_def.get('instructions', []):
        lines.extend(compile_instruction_csharp(_rewrite_event_refs(inst), 1, ctx))
    lines.append("    await Task.CompletedTask;")
    lines.append("}")
    return lines


def generate_clock_csharp() -> list[str]:
    """IAiLangClock + SystemAiLangClock (default) + FakeAiLangClock (test seam).

    Tests register FakeAiLangClock via `(override_time (day_of_week X))` /
    `(override_time (now "ISO-8601"))` so `GET_CURRENT clock.*` returns a
    deterministic value. DayOfWeek is lowercased to match AILang enum atoms.
    """
    return [
        "public interface IAiLangClock",
        "{",
        "    DateTime UtcNow();",
        "    DateTime Today();",
        "    string DayOfWeek();  // lowercased: \"monday\" .. \"sunday\"",
        "}",
        "",
        "public sealed class SystemAiLangClock : IAiLangClock",
        "{",
        "    public DateTime UtcNow() => DateTime.UtcNow;",
        "    public DateTime Today() => DateTime.UtcNow.Date;",
        "    public string DayOfWeek() => DateTime.UtcNow.DayOfWeek.ToString().ToLowerInvariant();",
        "}",
        "",
        "public sealed class FakeAiLangClock : IAiLangClock",
        "{",
        "    public DateTime FixedNow { get; set; } = DateTime.UtcNow;",
        "    public string? FixedDayOfWeek { get; set; }",
        "    public DateTime UtcNow() => FixedNow;",
        "    public DateTime Today() => FixedNow.Date;",
        "    public string DayOfWeek() => FixedDayOfWeek ?? FixedNow.DayOfWeek.ToString().ToLowerInvariant();",
        "}",
    ]


def generate_event_bus_csharp() -> list[str]:
    """Emit IEventBus + InMemoryEventBus (fire-and-forget, per-subscriber Task)."""
    return [
        "// ── EVENT BUS (in-process, fire-and-forget) ──",
        "public interface IEventBus",
        "{",
        "    void Subscribe(string eventName, Func<object?, IServiceProvider, Task> handler);",
        "    void Publish(string eventName, object? data);",
        "}",
        "",
        "public class InMemoryEventBus : IEventBus",
        "{",
        "    private readonly IServiceProvider _sp;",
        "    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, System.Collections.Generic.List<Func<object?, IServiceProvider, Task>>> _subs = new();",
        "    public InMemoryEventBus(IServiceProvider sp) { _sp = sp; }",
        "    public void Subscribe(string eventName, Func<object?, IServiceProvider, Task> handler)",
        "    {",
        "        var list = _subs.GetOrAdd(eventName, _ => new());",
        "        lock (list) { list.Add(handler); }",
        "    }",
        "    public void Publish(string eventName, object? data)",
        "    {",
        "        if (!_subs.TryGetValue(eventName, out var handlers)) return;",
        "        Func<object?, IServiceProvider, Task>[] snapshot;",
        "        lock (handlers) { snapshot = handlers.ToArray(); }",
        "        foreach (var h in snapshot)",
        "        {",
        "            _ = Task.Run(async () =>",
        "            {",
        "                try { await h(data, _sp); }",
        '                catch (Exception ex) { Console.WriteLine($"[EVENT] handler failed for {eventName}: {ex.Message}"); }',
        "            });",
        "        }",
        "    }",
        "}",
    ]


def generate_resilience_handler_csharp(policy: dict, include_retry: bool, indent: int = 1) -> list[str]:
    """Emit the `.AddResilienceHandler("name", b => { ... })` body for a policy.

    Order: CircuitBreaker (outer) → Retry → Timeout (innermost). Matches Polly v8
    recommended composition for HTTP pipelines. `include_retry=False` omits the
    retry strategy — used for the "no-retry" variant bound to non-idempotent
    calls.
    """
    pad = "    " * indent
    handler = f"{policy['name']}{'' if include_retry else '_no_retry'}"
    cb = policy.get('circuit_breaker')
    # The `(builder, context)` overload exposes ServiceProvider via ctx, which
    # we need when a circuit breaker is present to resolve the per-policy
    # CircuitBreakerManualControl.
    if cb:
        lines = [f'{pad}.AddResilienceHandler("{handler}", (b, _ctx) =>', f"{pad}{{"]
        lines.append(f'{pad}    var _manual_{policy["name"]} = _ctx.ServiceProvider.GetKeyedService<Polly.CircuitBreaker.CircuitBreakerManualControl>("{policy["name"]}");')
    else:
        lines = [f'{pad}.AddResilienceHandler("{handler}", b =>', f"{pad}{{"]
    if cb:
        lines.append(f"{pad}    b.AddCircuitBreaker(new Polly.CircuitBreaker.CircuitBreakerStrategyOptions<HttpResponseMessage>")
        lines.append(f"{pad}    {{")
        lines.append(f"{pad}        FailureRatio = 1.0,  // open when all requests in-window fail")
        lines.append(f"{pad}        MinimumThroughput = {cb['failure_threshold']},")
        lines.append(f"{pad}        SamplingDuration = TimeSpan.FromSeconds(30),")
        lines.append(f"{pad}        BreakDuration = TimeSpan.FromMilliseconds({cb['reset_timeout_ms']}),")
        lines.append(f"{pad}        ShouldHandle = new Polly.PredicateBuilder<HttpResponseMessage>()")
        lines.append(f"{pad}            .Handle<HttpRequestException>()")
        lines.append(f"{pad}            .HandleResult(r => (int)r.StatusCode >= 500),")
        lines.append(f'{pad}        ManualControl = _manual_{policy["name"]},')
        lines.append(f"{pad}    }});")
    retry = policy.get('retry')
    if retry and include_retry:
        bt = {
            'exponential': 'Polly.DelayBackoffType.Exponential',
            'linear': 'Polly.DelayBackoffType.Linear',
            'constant': 'Polly.DelayBackoffType.Constant',
        }.get(retry['backoff_type'], 'Polly.DelayBackoffType.Exponential')
        lines.append(f"{pad}    b.AddRetry(new Polly.Retry.RetryStrategyOptions<HttpResponseMessage>")
        lines.append(f"{pad}    {{")
        lines.append(f"{pad}        MaxRetryAttempts = {retry['max_attempts']},")
        lines.append(f"{pad}        Delay = TimeSpan.FromMilliseconds({retry['initial_ms']}),")
        lines.append(f"{pad}        BackoffType = {bt},")
        lines.append(f"{pad}        UseJitter = true,")
        lines.append(f"{pad}        ShouldHandle = new Polly.PredicateBuilder<HttpResponseMessage>()")
        lines.append(f"{pad}            .Handle<HttpRequestException>()")
        lines.append(f"{pad}            .Handle<Polly.Timeout.TimeoutRejectedException>()")
        lines.append(f"{pad}            .HandleResult(r => (int)r.StatusCode >= 500),")
        lines.append(f"{pad}    }});")
    timeout = policy.get('timeout')
    if timeout:
        lines.append(f"{pad}    b.AddTimeout(TimeSpan.FromMilliseconds({timeout['duration_ms']}));")
    lines.append(f"{pad}}})")
    return lines


def generate_service_client_csharp() -> list[str]:
    """Global helper: CallServiceAsync(factory, name, method, path, body) → Task<JsonNode?>.

    Returns the parsed JSON DOM (`JsonNode`) on 2xx, or null on transport / non-2xx.
    `JsonNode` is preferred over `dynamic` because it is AOT-compatible (critical
    since .NET 8), has no DLR overhead, and allows both indexer access
    (`node["price"]`) and strongly-typed conversion via `.Deserialize<T>()` once
    the caller knows the shape.
    """
    return [
        "// ── INTER-SERVICE HTTP HELPER ──",
        "// CallServiceAsync: returns JsonNode on 2xx, null on error.",
        "// Caller reads fields via node[\"name\"]?.GetValue<T>() or deserialises",
        "// the node into a known DTO.",
        "static async Task<System.Text.Json.Nodes.JsonNode?> CallServiceAsync(",
        "    IHttpClientFactory factory,",
        "    string clientName,",
        "    string method,",
        "    string path,",
        "    object? body)",
        "{",
        "    var client = factory.CreateClient(clientName);",
        "    using var req = new HttpRequestMessage(new HttpMethod(method), path);",
        "    if (body is not null)",
        "    {",
        "        req.Content = System.Net.Http.Json.JsonContent.Create(body);",
        "    }",
        "    try",
        "    {",
        "        using var resp = await client.SendAsync(req);",
        "        if (!resp.IsSuccessStatusCode) return null;",
        "        var json = await resp.Content.ReadAsStringAsync();",
        "        if (string.IsNullOrWhiteSpace(json)) return null;",
        "        return System.Text.Json.Nodes.JsonNode.Parse(json);",
        "    }",
        "    // Polly-specific exceptions propagate so the caller can map them to",
        "    // 504/503 via the AiLangError.Timeout / Unavailable factories.",
        "    catch (Polly.Timeout.TimeoutRejectedException) { throw; }",
        "    catch (Polly.CircuitBreaker.BrokenCircuitException) { throw; }",
        "    catch (Exception) { return null; }  // Everything else — treat as upstream NONE.",
        "}",
    ]


def generate_paginated_csharp() -> list[str]:
    """Generate the shared Paginated<T> record type for paginated store responses."""
    return [
        "// SHARED TYPE: Paginated<T>",
        "public record Paginated<T>",
        "{",
        '    [JsonPropertyName("data")]        public List<T> Data { get; init; } = [];',
        '    [JsonPropertyName("page")]        public int Page { get; init; }',
        '    [JsonPropertyName("limit")]       public int Limit { get; init; }',
        '    [JsonPropertyName("total")]       public long Total { get; init; }',
        '    [JsonPropertyName("total_pages")] public int TotalPages { get; init; }',
        "}",
    ]


def generate_postgres_store_csharp(store_name: str, store_def: dict, schema_def: dict) -> list[str]:
    """Generate a PostgreSQL-backed store using Npgsql.

    Emits:
      - I<Schema>Store interface (async, typed for the schema)
      - Postgres<Schema>Store : I<Schema>Store (uses NpgsqlDataSource + prepared statements)
      - MapRow static helper (used inline in handlers for STORE_FIND_BY / STORE_FILTER)
    """
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    mapping = list(store_def.get('mapping', {}).items())
    if not mapping:
        return [f"// WARN: store {store_name} has no (mapping ...) — skipping PG generation"]

    # Partition fields: db-managed vs writable (come from DTO).
    # A field is db-managed when the schema marks it auto_gen OR declares a
    # default — letting the column DEFAULT apply (e.g. `active bool (default true)`
    # and `created_at timestamp (auto_gen)`). Writable fields are what the
    # INSERT actually sends; SELECT/RETURNING still reads every column.
    def _is_db_managed(field_name: str) -> bool:
        sfield = schema_def.get('fields', {}).get(field_name, {})
        return bool(sfield.get('auto_gen')) or sfield.get('default') is not None
    auto_fields = [(f, m) for f, m in mapping if _is_db_managed(f)]
    writable_fields = [(f, m) for f, m in mapping if not _is_db_managed(f)]

    all_columns_sql = ", ".join(m['column'] for _, m in mapping)
    writable_column_sql = ", ".join(m['column'] for _, m in writable_fields)
    writable_placeholders = ", ".join(f"@{f}" for f, _ in writable_fields)

    # Multi-line SQL — the raw-string-literal emitter turns these into readable
    # C# """...""" literals. Single-line SQLs stay as plain "..." strings.
    insert_sql = (
        f"INSERT INTO {table} ({writable_column_sql})\n"
        f"VALUES ({writable_placeholders})\n"
        f"RETURNING {all_columns_sql}"
    )
    set_clauses = [f"{m['column']}=@{f}" for f, m in writable_fields]
    if 'updated_at' in dict(mapping):
        set_clauses.append("updated_at=NOW()")
    update_sql = (
        f"UPDATE {table}\n"
        f"SET {', '.join(set_clauses)}\n"
        f"WHERE id=@id\n"
        f"RETURNING {all_columns_sql}"
    )
    select_by_id_sql = f"SELECT {all_columns_sql}\nFROM {table}\nWHERE id=@id"
    select_all_sql = (
        f"SELECT {all_columns_sql}\n"
        f"FROM {table}\n"
        f"ORDER BY id\n"
        f"LIMIT @limit OFFSET @offset"
    )
    count_all_sql = f"SELECT COUNT(*) FROM {table}"
    delete_sql = f"DELETE FROM {table} WHERE id=@id"

    lines = []
    lines.append(f"// STORE: Postgres{schema}Store (from bytecode mapping)")
    lines.append(f"public interface I{schema}Store")
    lines.append("{")
    lines.append("    Npgsql.NpgsqlDataSource DataSource { get; }")
    lines.append(f"    Task<Paginated<{schema}>> ListAllAsync(int page, int limit, int maxLimit);")
    lines.append(f"    Task<{schema}?> FindAsync(Guid id);")
    lines.append(f"    Task<{schema}> InsertAsync({schema}Dto data);")
    lines.append(f"    Task<{schema}?> UpdateAsync(Guid id, {schema}Dto data);")
    lines.append(f"    Task<bool> DeleteAsync(Guid id);")
    lines.append("}")
    lines.append("")

    # Primary constructor (C# 12+): `class Foo(Dep d) : IFoo` — the `ds` param is
    # an implicit private field accessible throughout the class body.
    lines.append(f"public class Postgres{schema}Store(Npgsql.NpgsqlDataSource ds) : I{schema}Store")
    lines.append("{")
    lines.append("    public Npgsql.NpgsqlDataSource DataSource => ds;")
    lines.append("")

    # MapRow
    lines.append(f"    public static {schema} MapRow(Npgsql.NpgsqlDataReader reader)")
    lines.append("    {")
    lines.append("        return new()  // target-typed: compiler infers Schema from method return type")
    lines.append("        {")
    for idx, (field, m) in enumerate(mapping):
        getter = sql_type_to_reader_getter(m['sql_type'], idx)
        prop = pascal_case(field)
        lines.append(f"            {prop} = {getter},")
    lines.append("        };")
    lines.append("    }")
    lines.append("")

    # ListAllAsync (paginated)
    lines.append(f"    public async Task<Paginated<{schema}>> ListAllAsync(int page, int limit, int maxLimit)")
    lines.append("    {")
    lines.append("        if (maxLimit < 1) maxLimit = 100;")
    lines.append("        if (limit < 1) limit = 20;")
    lines.append("        if (limit > maxLimit) limit = maxLimit;")
    lines.append("        if (page < 1) page = 1;")
    lines.append("        var offset = (page - 1) * limit;")
    lines.append("        await using var conn = await ds.OpenConnectionAsync();")
    lines.append("        long total;")
    lines.append("        {")
    lines.append(f'            await using var countCmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(count_all_sql, 3)}, conn);')
    lines.append("            total = Convert.ToInt64(await countCmd.ExecuteScalarAsync());")
    lines.append("        }")
    lines.append(f"        List<{schema}> items = new(limit);  // pre-allocated capacity hint from limit")
    lines.append("        {")
    lines.append(f'            await using var dataCmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(select_all_sql, 3)}, conn);')
    lines.append('            dataCmd.Parameters.AddWithValue("@limit", limit);')
    lines.append('            dataCmd.Parameters.AddWithValue("@offset", offset);')
    lines.append("            await using var reader = await dataCmd.ExecuteReaderAsync();")
    lines.append("            while (await reader.ReadAsync())")
    lines.append("                items.Add(MapRow(reader));")
    lines.append("        }")
    lines.append("        var totalPages = limit == 0 ? 0 : (int)Math.Ceiling((double)total / limit);")
    lines.append("        return new() { Data = items, Page = page, Limit = limit, Total = total, TotalPages = totalPages };")
    lines.append("    }")
    lines.append("")

    # FindAsync
    lines.append(f"    public async Task<{schema}?> FindAsync(Guid id)")
    lines.append("    {")
    lines.append("        await using var conn = await ds.OpenConnectionAsync();")
    lines.append(f'        await using var cmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(select_by_id_sql, 2)}, conn);')
    lines.append('        cmd.Parameters.AddWithValue("@id", id);')
    lines.append("        await using var reader = await cmd.ExecuteReaderAsync();")
    lines.append("        if (!await reader.ReadAsync()) return null;")
    lines.append("        return MapRow(reader);")
    lines.append("    }")
    lines.append("")

    # InsertAsync
    lines.append(f"    public async Task<{schema}> InsertAsync({schema}Dto data)")
    lines.append("    {")
    lines.append("        await using var conn = await ds.OpenConnectionAsync();")
    lines.append(f'        await using var cmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(insert_sql, 2)}, conn);')
    for f, m in writable_fields:
        cs_type = sql_type_to_csharp(m['sql_type'])
        prop = pascal_case(f)
        field_required = schema_def.get('fields', {}).get(f, {}).get('required', False)
        if cs_type == 'string' and not field_required:
            # DTO prop is nullable string — fall back to DBNull for SQL
            lines.append(f'        cmd.Parameters.AddWithValue("@{f}", (object?)data.{prop} ?? DBNull.Value);')
        else:
            lines.append(f'        cmd.Parameters.AddWithValue("@{f}", data.{prop});')
    lines.append("        await using var reader = await cmd.ExecuteReaderAsync();")
    lines.append("        await reader.ReadAsync();")
    lines.append("        return MapRow(reader);")
    lines.append("    }")
    lines.append("")

    # UpdateAsync
    lines.append(f"    public async Task<{schema}?> UpdateAsync(Guid id, {schema}Dto data)")
    lines.append("    {")
    lines.append("        await using var conn = await ds.OpenConnectionAsync();")
    lines.append(f'        await using var cmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(update_sql, 2)}, conn);')
    lines.append('        cmd.Parameters.AddWithValue("@id", id);')
    for f, m in writable_fields:
        cs_type = sql_type_to_csharp(m['sql_type'])
        prop = pascal_case(f)
        field_required = schema_def.get('fields', {}).get(f, {}).get('required', False)
        if cs_type == 'string' and not field_required:
            # DTO prop is nullable string — fall back to DBNull for SQL
            lines.append(f'        cmd.Parameters.AddWithValue("@{f}", (object?)data.{prop} ?? DBNull.Value);')
        else:
            lines.append(f'        cmd.Parameters.AddWithValue("@{f}", data.{prop});')
    lines.append("        await using var reader = await cmd.ExecuteReaderAsync();")
    lines.append("        if (!await reader.ReadAsync()) return null;")
    lines.append("        return MapRow(reader);")
    lines.append("    }")
    lines.append("")

    # DeleteAsync
    lines.append(f"    public async Task<bool> DeleteAsync(Guid id)")
    lines.append("    {")
    lines.append("        await using var conn = await ds.OpenConnectionAsync();")
    lines.append(f'        await using var cmd = new Npgsql.NpgsqlCommand({emit_sql_literal_csharp(delete_sql, 2)}, conn);')
    lines.append('        cmd.Parameters.AddWithValue("@id", id);')
    lines.append("        return await cmd.ExecuteNonQueryAsync() > 0;")
    lines.append("    }")
    lines.append("}")
    return lines


def generate_row_mappers_csharp(bc) -> list[str]:
    """Emit ReadRow<Schema>(NpgsqlDataReader) helpers for schemas used by SQL_QUERY/SQL_STREAM.

    Uses a best-effort mapping based on the reader's column names to schema fields.
    """
    lines = []
    targets = set()
    for h in bc.handlers.values():
        for inst in h.get('instructions', []):
            if inst.get('op') in ('SQL_QUERY', 'SQL_STREAM'):
                t = (h.get('returns') or {}).get('type')
                if t:
                    targets.add(t)

    # Numeric aggregations in Postgres come back wider than the schema declares
    # (COUNT → bigint, SUM(int) → bigint, ROUND(avg) → numeric), and a raw C#
    # `(int)boxedLong` cast throws InvalidCastException. `Convert.ToXxx` coerces
    # across the numeric tower instead, so schema `int` tolerates a bigint column.
    _cs_convert = {
        'int':     'Convert.ToInt32',
        'long':    'Convert.ToInt64',
        'decimal': 'Convert.ToDecimal',
        'float':   'Convert.ToDouble',
        'bool':    'Convert.ToBoolean',
    }
    for tname in targets:
        sd = bc.schemas.get(tname)
        if not sd:
            continue
        lines.append(f"// ROW MAPPER: ReadRow{tname}")
        lines.append(f"static {tname} ReadRow{tname}(Npgsql.NpgsqlDataReader reader)")
        lines.append("{")
        lines.append(f"    return new {tname}")
        lines.append("    {")
        for field_name, field_def in sd.get('fields', {}).items():
            prop = pascal_case(field_name)
            ftype = field_def.get('type')
            cs_type = TypeMapper.to_csharp(ftype)
            ordinal_expr = f'reader.GetOrdinal("{field_name}")'
            read_expr = f'reader.GetValue({ordinal_expr})'
            conv = _cs_convert.get(ftype)
            cast_expr = f'{conv}({read_expr})' if conv else f'({cs_type}){read_expr}'
            lines.append(f'        {prop} = reader.IsDBNull({ordinal_expr}) ? default : {cast_expr},')
        lines.append("    };")
        lines.append("}")
        lines.append("")
    return lines


def _autogen_default_csharp(ftype: str) -> str:
    """Default expression for an auto_gen field on a fresh Insert."""
    if ftype == 'uuid':
        return 'Guid.NewGuid()'
    if ftype == 'timestamp':
        return 'DateTime.UtcNow'
    if ftype == 'int':
        return '0'
    if ftype == 'bool':
        return 'false'
    if ftype == 'string':
        return '""'
    return 'default!'


def generate_store_csharp(schema: str, schema_def: dict, store_def: dict = None) -> list[str]:
    """Generate C# in-memory store from AILang store definition."""
    fields = schema_def.get('fields', {})
    dto_fields = {k: v for k, v in fields.items() if not v.get('auto_gen')}
    auto_gen_fields = {k: v for k, v in fields.items() if v.get('auto_gen')}

    # Resolve the primary key from the store's index definition.
    index = (store_def or {}).get('index', {}) or {}
    pk_field = index.get('field', 'id')
    pk_type_ailang = fields.get(pk_field, {}).get('type', 'uuid')
    pk_cs_type = TypeMapper.to_csharp(pk_type_ailang)
    pk_prop = pascal_case(pk_field)
    pk_is_auto = fields.get(pk_field, {}).get('auto_gen', False)

    lines = []
    lines.append(f"// STORE: InMemory{schema}Store (from bytecode)")
    lines.append(f"public interface I{schema}Store")
    lines.append("{")
    lines.append(f"    List<{schema}> ListAll();")
    lines.append(f"    {schema}? Find({pk_cs_type} {pk_field});")
    lines.append(f"    {schema} Insert({schema}Dto data);")
    lines.append(f"    {schema}? Update({pk_cs_type} {pk_field}, {schema}Dto data);")
    lines.append(f"    bool Delete({pk_cs_type} {pk_field});")
    lines.append("    void Reset();  // test seam — RESET_STORE in (tests ... (clean ...))")
    lines.append(f"    void Seed(System.Collections.Generic.IEnumerable<{schema}> items);  // test seam — seed_data in (tests ... (setup ...))")
    lines.append("}")
    lines.append("")

    lines.append(f"public class InMemory{schema}Store : I{schema}Store")
    lines.append("{")
    lines.append(f"    private readonly ConcurrentDictionary<{pk_cs_type}, {schema}> _data = new();")
    lines.append("")
    lines.append(f"    public List<{schema}> ListAll() => [.. _data.Values];")
    lines.append(f"    public {schema}? Find({pk_cs_type} {pk_field}) => _data.GetValueOrDefault({pk_field});")
    lines.append("")

    # Insert: set all auto_gen fields + DTO fields
    lines.append(f"    public {schema} Insert({schema}Dto data)")
    lines.append("    {")
    lines.append(f"        {schema} item = new()")
    lines.append("        {")
    for field_name, field_def in auto_gen_fields.items():
        prop = pascal_case(field_name)
        lines.append(f"            {prop} = {_autogen_default_csharp(field_def['type'])},")
    for field_name in dto_fields:
        prop = pascal_case(field_name)
        lines.append(f"            {prop} = data.{prop}!,")
    lines.append("        };")
    lines.append(f"        _data[item.{pk_prop}] = item;")
    lines.append("        return item;")
    lines.append("    }")
    lines.append("")

    lines.append(f"    public {schema}? Update({pk_cs_type} {pk_field}, {schema}Dto data)")
    lines.append("    {")
    lines.append(f"        if (!_data.TryGetValue({pk_field}, out var existing)) return null;")
    lines.append(f"        {schema} item = new()")
    lines.append("        {")
    lines.append(f"            {pk_prop} = {pk_field},")
    for field_name, field_def in auto_gen_fields.items():
        if field_name == pk_field:
            continue
        prop = pascal_case(field_name)
        if field_name == 'updated_at':
            lines.append(f"            {prop} = DateTime.UtcNow,")
        else:
            lines.append(f"            {prop} = existing.{prop},")
    for field_name in dto_fields:
        if field_name == pk_field:
            continue
        prop = pascal_case(field_name)
        lines.append(f"            {prop} = data.{prop}!,")
    lines.append("        };")
    lines.append(f"        _data[{pk_field}] = item;")
    lines.append("        return item;")
    lines.append("    }")
    lines.append("")
    lines.append(f"    public bool Delete({pk_cs_type} {pk_field}) => _data.TryRemove({pk_field}, out _);")
    lines.append("    public void Reset() => _data.Clear();")
    # Seed inserts records at their declared PK without touching auto-gen
    # defaults. Used only by tests' (setup (seed_data …)). Replaces on conflict.
    lines.append(f"    public void Seed(System.Collections.Generic.IEnumerable<{schema}> items)")
    lines.append("    {")
    lines.append("        foreach (var item in items)")
    lines.append(f"            _data[item.{pk_prop}] = item;")
    lines.append("    }")
    lines.append("}")
    return lines


def generate_error_model_csharp(custom_errors: dict) -> list[str]:
    """Generate the AiLangError model for C#."""
    lines = []
    lines.append("// ERROR MODEL: AILang native errors")
    lines.append("public record AiLangError")
    lines.append("{")
    lines.append('    [JsonPropertyName("error")]')
    lines.append("    public ErrorDetail Error { get; init; } = new();")
    lines.append("")

    # Standard error factories
    errors = [
        ("NotFound", [("entity", "string"), ("id", "string")], "NOT_FOUND",
         'new() { Type = "NOT_FOUND", Entity = entity, Id = id, Message = $"{entity} with id {id} not found" }'),
        ("Validation", [("field", "string"), ("reason", "string")], "VALIDATION",
         'new() { Type = "VALIDATION", Field = field, Message = $"validation failed: {field} — {reason}" }'),
        ("BusinessRule", [("rule", "string"), ("detail", "string")], "BUSINESS_RULE",
         'new() { Type = "BUSINESS_RULE", Rule = rule, Message = detail }'),
        ("Unauthorized", [], "UNAUTHORIZED",
         'new() { Type = "UNAUTHORIZED", Message = "authentication required" }'),
        ("InvalidBody", [("schema", "string"), ("detail", "string")], "INVALID_BODY",
         'new() { Type = "INVALID_BODY", Message = $"body does not match schema {schema}: {detail}" }'),
        ("InvalidParam", [("name", "string"), ("expected", "string")], "INVALID_PARAM",
         'new() { Type = "INVALID_PARAM", Field = name, Message = $"parameter {name} must be {expected}" }'),
        ("Unavailable", [("service", "string")], "UNAVAILABLE",
         'new() { Type = "UNAVAILABLE", Message = $"service {service} is unavailable" }'),
        ("Timeout", [("service", "string")], "TIMEOUT",
         'new() { Type = "TIMEOUT", Message = $"service {service} timed out" }'),
        ("Internal", [("detail", "string")], "INTERNAL",
         'new() { Type = "INTERNAL", Message = detail }'),
    ]

    for name, params, _, init in errors:
        param_str = ", ".join(f"string {p}" for p, _ in params)
        lines.append(f"    public static AiLangError {name}({param_str}) => new()")
        lines.append(f"    {{ Error = {init} }};")
        lines.append("")

    # Custom error factories — one per definition in the bytecode.
    for err_type, entry in (custom_errors or {}).items():
        factory = err_name_to_factory(err_type)
        tag = err_type_tag(err_type)
        declared = entry.get('params', []) or []
        template = entry.get('message_template', tag)
        param_camel = {p: camel_case(p) for p in declared}
        sig_params = ", ".join(f"object {param_camel[p]}" for p in declared)
        msg_body = template_to_csharp_interpolated(template, param_camel)
        lines.append(f"    public static AiLangError {factory}({sig_params}) => new()")
        lines.append(f'    {{ Error = new() {{ Type = "{tag}", Message = $"{msg_body}" }} }};')
        lines.append("")

    lines.append("}")
    lines.append("")
    lines.append("public record ErrorDetail")
    lines.append("{")
    for prop in [("type", "Type"), ("message", "Message")]:
        lines.append(f'    [JsonPropertyName("{prop[0]}")] public string {prop[1]} {{ get; init; }} = "";')
    for prop in [("entity", "Entity"), ("id", "Id"), ("field", "Field"), ("rule", "Rule")]:
        lines.append(f'    [JsonPropertyName("{prop[0]}")] [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? {prop[1]} {{ get; init; }}')
    lines.append("}")

    return lines


# ── Go Generator ──

def _go_path_template(path: str, handler_def: dict) -> tuple:
    """Rewrite ':name' route params to ServeMux '{name}' syntax; uuid/int get '__raw' suffix."""
    import re
    handler_params = handler_def.get('params', {}) or {}
    pattern = re.compile(r':(\w+)')
    typed = []
    new_path = path
    for name in pattern.findall(path):
        ailang_type = handler_params.get(name, 'string')
        if ailang_type in ('uuid', 'int'):
            route_var = f"{name}__raw"
        else:
            route_var = name
        new_path = new_path.replace(f':{name}', '{' + route_var + '}', 1)
        typed.append({'name': name, 'route_var': route_var, 'type': ailang_type})
    return new_path, typed


def generate_go(bc, output_dir: str):
    """Generate a complete Go project from bytecode (in-memory or PostgreSQL-backed)."""
    os.makedirs(output_dir, exist_ok=True)

    service_name = bc.service['name']
    port = bc.service['port']

    pg_stores = {n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'}
    is_pg = bool(pg_stores)

    jwt_env = _resolve_env_var(bc, bc.auth.get('secret_ref', 'jwt_secret') if bc.auth else 'jwt_secret', 'JWT_SECRET')
    db_conn_env = _resolve_env_var(bc, 'db_connection', 'DB_CONNECTION_STRING')

    # Primary store (drives handler codegen — matches C# generator)
    primary_store_name, primary_store_def = next(iter(bc.stores.items())) if bc.stores else (None, None)
    primary_schema = primary_store_def['schema'] if primary_store_def else None

    # Pre-scan handlers to decide which optional imports we need — must descend
    # into MATCH/BRANCH cases, ITERATE bodies, and PARALLEL fibers. Guard each
    # recursive step because instruction bodies can occasionally hold raw atoms.
    def _walk_ops(h):
        def _walk(insts):
            for inst in insts or []:
                if not isinstance(inst, dict):
                    continue
                yield inst.get('op')
                for case_insts in (inst.get('cases') or {}).values():
                    if isinstance(case_insts, list):
                        yield from _walk(case_insts)
                body = inst.get('body') or []
                if body:
                    yield from _walk(body)
                for fiber in inst.get('fibers', []) or []:
                    yield from _walk(fiber.get('instructions', []) or [])
        yield from _walk(h.get('instructions', []))

    all_ops = set()
    for h in bc.handlers.values():
        for op in _walk_ops(h):
            all_ops.add(op)
            # Also walk nested COMPUTE_FN args for math detection downstream
    has_in_memory_store = any(s.get('provider') != 'PostgreSQL' for s in bc.stores.values())
    # `math` is only needed for round/floor/ceil/abs — not length/take_last
    # which just use len() and slicing. Walk COMPUTE_FN args to check.
    _math_fns = {'round', 'floor', 'ceil', 'abs'}
    def _has_math_compute_fn(insts):
        for i in insts:
            if not isinstance(i, dict):
                continue
            if i.get('op') == 'COMPUTE_FN' and i.get('function') in _math_fns:
                return True
            for k in ('body',):
                if isinstance(i.get(k), list) and _has_math_compute_fn(i[k]):
                    return True
            if isinstance(i.get('cases'), dict):
                for v in i['cases'].values():
                    if isinstance(v, list) and _has_math_compute_fn(v):
                        return True
        return False
    needs_math = any(_has_math_compute_fn(h.get('instructions', []) or []) for h in bc.handlers.values())
    needs_http_client = 'CALL_SERVICE' in all_ops
    needs_resilience_go = needs_http_client and bool((bc.resilience or {}).get('policies'))
    needs_events = ('EMIT_EVENT' in all_ops) or any(
        h.get('type') == 'EVENT_HANDLER' for h in bc.handlers.values()
    )
    needs_sagas = bool(bc.sagas) or 'SAGA_COMPENSATE' in all_ops
    needs_parallel = 'PARALLEL' in all_ops or 'AWAIT_ALL' in all_ops

    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("// AILang Transpiler Output — Go 1.25")
    lines.append(f"// Source: {service_name} (AILang Bytecode v{bc.raw.get('version', '0.8')})")
    lines.append("// Auto-generated by AILang Transpiler")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("")
    lines.append("package main")
    lines.append("")
    lines.append("import (")
    if needs_http_client:
        lines.append('\t"bytes"')
    if is_pg or needs_http_client or needs_events or needs_sagas:
        lines.append('\t"context"')
    lines.append('\t"encoding/json"')
    # `errors` is used for errors.Is(context.DeadlineExceeded / gobreaker.ErrOpenState)
    # in the resilient callService, and by PG store error mapping.
    if is_pg or needs_resilience_go:
        lines.append('\t"errors"')
    lines.append('\t"fmt"')
    if needs_http_client:
        lines.append('\t"io"')
    lines.append('\t"log"')
    lines.append('\t"log/slog"')
    if needs_math:
        lines.append('\t"math"')
    lines.append('\t"net/http"')
    if is_pg or needs_http_client:
        lines.append('\t"os"')
    lines.append('\t"strconv"')
    lines.append('\t"strings"')
    if has_in_memory_store or needs_resilience_go or needs_events or needs_parallel:
        lines.append('\t"sync"')
    lines.append('\t"time"')
    lines.append("")
    lines.append('\t"github.com/google/uuid"')
    if needs_resilience_go:
        lines.append('\t"github.com/sony/gobreaker/v2"')
    if is_pg:
        lines.append('\t"github.com/jackc/pgx/v5"')
        lines.append('\t"github.com/jackc/pgx/v5/pgxpool"')
    lines.append(")")
    lines.append("")

    # Keep references used — Go barks on unused imports
    lines.append("var _ = strings.HasPrefix  // keep strings import")
    lines.append("var _ = strconv.Atoi      // keep strconv import")
    lines.append("")

    # Schemas
    for schema_name, schema_def in bc.schemas.items():
        lines.extend(generate_schema_go(schema_name, schema_def))
        lines.append("")

    # Paginated[T] when needed
    if is_pg or any(
        i.get('op') in ('STORE_LIST_ALL', 'STORE_FILTER') and i.get('paginate')
        for h in bc.handlers.values()
        for i in h.get('instructions', [])
    ):
        lines.extend(generate_paginated_go())
        lines.append("")

    # Error model (shared)
    lines.extend(generate_error_model_go(bc.custom_errors))
    lines.append("")

    # ── TEST SEAMS ──
    # These are package-level state that tests can swap in via (setup ...) ops.
    # `buildHandler()` is responsible for resetting them to fresh defaults each
    # call, so successive tests don't leak state across the httptest server.
    lines.append("// ── TEST SEAMS ──")
    lines.append("// _clock is used by GET_CURRENT clock.* — tests swap in FakeClock via (override_time …).")
    lines.append("type AiLangClock interface {")
    lines.append("\tNow() time.Time")
    lines.append("\tToday() time.Time")
    lines.append("\tDayOfWeek() time.Weekday")
    lines.append("}")
    lines.append("type SystemClock struct{}")
    lines.append("func (SystemClock) Now() time.Time        { return time.Now().UTC() }")
    lines.append("func (SystemClock) Today() time.Time      { return time.Now().UTC().Truncate(24 * time.Hour) }")
    lines.append("func (SystemClock) DayOfWeek() time.Weekday { return time.Now().UTC().Weekday() }")
    lines.append("")
    lines.append("// FakeClock — used in tests via (override_time (now ISO) (day_of_week X)).")
    lines.append("// Zero values fall through to SystemClock so tests can override only what they need.")
    lines.append("type FakeClock struct {")
    lines.append("\tFixedNow       time.Time")
    lines.append("\tFixedDayOfWeek *time.Weekday")
    lines.append("}")
    lines.append("func (f FakeClock) Now() time.Time {")
    lines.append("\tif !f.FixedNow.IsZero() { return f.FixedNow }")
    lines.append("\treturn time.Now().UTC()")
    lines.append("}")
    lines.append("func (f FakeClock) Today() time.Time {")
    lines.append("\tif !f.FixedNow.IsZero() { return f.FixedNow.Truncate(24 * time.Hour) }")
    lines.append("\treturn time.Now().UTC().Truncate(24 * time.Hour)")
    lines.append("}")
    lines.append("func (f FakeClock) DayOfWeek() time.Weekday {")
    lines.append("\tif f.FixedDayOfWeek != nil { return *f.FixedDayOfWeek }")
    lines.append("\treturn time.Now().UTC().Weekday()")
    lines.append("}")
    lines.append("")
    lines.append("var _clock AiLangClock = SystemClock{}")
    lines.append("// _stores exposes in-memory stores (or the PG store wrapper) by schema name")
    lines.append("// so tests can Seed()/reach the datasource without DI.")
    lines.append("var _stores = map[string]any{}")
    if is_pg:
        lines.append("// _pgPool — shared pgx pool, lazy-init in buildHandler() on first call.")
        lines.append("var _pgPool *pgxpool.Pool")
    if (bc.resilience or {}).get('policies'):
        lines.append("// _forcedCircuitStates[policy] = \"OPEN\" bypasses the real CB in callService —")
        lines.append("// used by (force_circuit_state (policy_name OPEN)) in test setups.")
        lines.append("var _forcedCircuitStates = map[string]string{}")
    lines.append("")

    # Stores
    for store_name, store_def in bc.stores.items():
        schema = store_def['schema']
        schema_def = bc.schemas[schema]
        if store_def.get('provider') == 'PostgreSQL':
            lines.extend(generate_postgres_store_go(store_name, store_def, schema_def))
        else:
            lines.extend(generate_store_go(schema, schema_def, store_def))
        lines.append("")

    # ReadRow<T> helpers for SQL_QUERY/SQL_STREAM
    if is_pg:
        lines.extend(generate_row_mappers_go(bc))

    # Auth helper
    lines.append("// AUTH: Bearer token validation")
    lines.append("func isAuthenticated(r *http.Request) bool {")
    lines.append('\tauth := r.Header.Get("Authorization")')
    lines.append('\treturn strings.HasPrefix(auth, "Bearer ") && len(auth) > 7')
    lines.append("}")
    lines.append("")

    # HTTP helpers
    lines.append("// HTTP helpers")
    lines.append("func respondJSON(w http.ResponseWriter, status int, data any) {")
    lines.append('\tw.Header().Set("Content-Type", "application/json")')
    lines.append("\tw.WriteHeader(status)")
    lines.append("\tif data != nil { json.NewEncoder(w).Encode(data) }")
    lines.append("}")
    lines.append("")
    lines.append("func respondError(w http.ResponseWriter, err *AiLangError) {")
    lines.append("\trespondJSON(w, err.StatusCode(), err)")
    lines.append("}")
    lines.append("")
    # _sliceContains — used by BRANCH IN/NOT_IN against slices. Emitted
    # unconditionally (it's small and always Go-valid); callers that don't
    # need it cost a dead func, which Go is happy to accept.
    lines.append("func _sliceContains(haystack any, needle any) bool {")
    lines.append("\tswitch h := haystack.(type) {")
    lines.append("\tcase []string:")
    lines.append("\t\ts, _ := needle.(string)")
    lines.append("\t\tfor _, v := range h { if v == s { return true } }")
    lines.append("\tcase []any:")
    lines.append("\t\tfor _, v := range h { if v == needle { return true } }")
    lines.append("\t}")
    lines.append("\treturn false")
    lines.append("}")
    lines.append("")

    # Middlewares
    cors_origins = bc.cors.get('origins', ['*'])
    cors_methods = ", ".join(bc.cors.get('methods', []))
    cors_headers = ", ".join(bc.cors.get('headers', []))
    lines.append("// CORS middleware (from bytecode)")
    lines.append("func corsMiddleware(next http.Handler) http.Handler {")
    lines.append("\treturn http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {")
    lines.append(f'\t\tw.Header().Set("Access-Control-Allow-Origin", "{cors_origins[0]}")')
    lines.append(f'\t\tw.Header().Set("Access-Control-Allow-Methods", "{cors_methods}")')
    lines.append(f'\t\tw.Header().Set("Access-Control-Allow-Headers", "{cors_headers}")')
    lines.append(f'\t\tw.Header().Set("Access-Control-Max-Age", "{bc.cors.get("max_age", 3600)}")')
    lines.append('\t\tif r.Method == "OPTIONS" { w.WriteHeader(204); return }')
    lines.append("\t\tnext.ServeHTTP(w, r)")
    lines.append("\t})")
    lines.append("}")
    lines.append("")

    lines.append("// TRACING middleware (native observability)")
    lines.append("func tracingMiddleware(next http.Handler) http.Handler {")
    lines.append("\treturn http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {")
    lines.append('\t\ttraceID := uuid.New().String()[:16]')
    lines.append('\t\tw.Header().Set("X-Trace-Id", traceID)')
    lines.append("\t\tstart := time.Now()")
    lines.append('\t\tslog.Info("trace start", "trace_id", traceID, "method", r.Method, "path", r.URL.Path)')
    lines.append("\t\trw := &statusWriter{ResponseWriter: w, status: 200}")
    lines.append("\t\tnext.ServeHTTP(rw, r)")
    lines.append('\t\tslog.Info("trace end", "trace_id", traceID, "method", r.Method, "path", r.URL.Path, "status", rw.status, "duration_ms", time.Since(start).Milliseconds())')
    lines.append("\t})")
    lines.append("}")
    lines.append("")
    lines.append("type statusWriter struct { http.ResponseWriter; status int }")
    lines.append("func (w *statusWriter) WriteHeader(s int) { w.status = s; w.ResponseWriter.WriteHeader(s) }")
    lines.append("")

    # SERVICE_REF dependencies drive the Router.serviceURLs map used by CALL_SERVICE.
    service_refs_go = {n: d for n, d in (bc.dependencies or {}).items() if d.get('type') == 'SERVICE_REF'}
    uses_call_service_anywhere = any(
        inst.get('op') == 'CALL_SERVICE'
        for h in bc.handlers.values()
        for inst in h.get('instructions', [])
    ) or bool(service_refs_go)

    event_handlers_go = [
        (n, h) for n, h in bc.handlers.items() if h.get('type') == 'EVENT_HANDLER'
    ]
    # EMIT_EVENT can be nested in MATCH/BRANCH bodies — mirror the recursive walker.
    def _walk_all_ops_in_handler_go(handler):
        def _walk(insts):
            for inst in insts:
                yield inst.get('op')
                for case_insts in (inst.get('cases') or {}).values():
                    if isinstance(case_insts, list):
                        yield from _walk(case_insts)
                for sub in inst.get('body', []) or []:
                    if isinstance(sub, dict):
                        yield from _walk([sub])
                for fiber in inst.get('fibers', []) or []:
                    yield from _walk(fiber.get('instructions', []) or [])
        yield from _walk(handler.get('instructions', []))
    uses_events_go = bool(event_handlers_go) or any(
        op == 'EMIT_EVENT'
        for h in bc.handlers.values()
        for op in _walk_all_ops_in_handler_go(h)
    )

    # Router
    lines.append("// ROUTER (from bytecode routes)")
    lines.append("type Router struct {")
    if is_pg:
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            lines.append(f"\tstore I{schema}Store")
    else:
        for store_name, store_def in bc.stores.items():
            lines.append(f"\tstore *{store_def['schema']}Store")
    if uses_call_service_anywhere:
        lines.append("\tserviceURLs map[string]string")
    if uses_events_go:
        lines.append("\tbus *EventBus")
    lines.append("}")
    lines.append("")

    # Package-level aliases so EVENT_HANDLERs (free funcs — no Router receiver)
    # can reach the service URL map and event bus without smuggling Router in
    # through the handler signature. Set in buildHandler / main after creation.
    if uses_call_service_anywhere:
        lines.append("var _serviceURLs map[string]string")
    if uses_events_go:
        lines.append("var _bus *EventBus")
    if uses_call_service_anywhere or uses_events_go:
        lines.append("")

    if uses_events_go:
        lines.extend(generate_event_bus_go())
        lines.append("")

    if uses_call_service_anywhere:
        lines.extend(generate_service_client_go(bc))
        lines.append("")

    # Saga compensators — free functions referenced by SAGA_COMPENSATE.
    if bc.sagas:
        lines.extend(generate_saga_compensators_go(bc))

    # EVENT_HANDLER functions — standalone, take (ctx, data any), not methods on Router.
    for h_name, h_def in event_handlers_go:
        lines.extend(_emit_event_handler_go(h_name, h_def, bc))
        lines.append("")

    # Regular HTTP handlers on Router. Skip EVENT_HANDLERs — they're already emitted
    # above as free functions and subscribed in main().
    for handler_name, handler_def in bc.handlers.items():
        if handler_def.get('type') == 'EVENT_HANDLER':
            continue
        lines.extend(generate_handler_go(handler_name, handler_def, bc, is_pg, primary_store_def))
        lines.append("")

    # Mux builder — uses Go 1.22 ServeMux method+path patterns
    lines.append("func (rt *Router) mux() *http.ServeMux {")
    lines.append("\tm := http.NewServeMux()")
    for route in bc.routes:
        method = route['method']
        handler_name = route['handler']
        handler_def = bc.handlers[handler_name]
        path_tpl, _typed = _go_path_template(route['path'], handler_def)
        lines.append(f'\tm.HandleFunc("{method} {path_tpl}", rt.{camel_case(handler_name)})')
    # Token endpoint for testing
    lines.append('\tm.HandleFunc("POST /auth/token", func(w http.ResponseWriter, r *http.Request) {')
    lines.append('\t\trespondJSON(w, 200, map[string]string{"token": "dev-token-" + uuid.New().String()[:8]})')
    lines.append("\t})")
    lines.append("\treturn m")
    lines.append("}")
    lines.append("")

    # buildHandler — test seam. Exports the full middleware stack so that
    # main_test.go can wrap it with httptest.NewServer without touching main().
    # Postgres is skipped here: the tests target in-memory stores only.
    lines.append("// buildHandler wires the full service graph and returns the HTTP handler.")
    lines.append("// Used by main() and by main_test.go (httptest). Not for PG-backed services.")
    lines.append("func buildHandler() http.Handler {")
    # Reset test seams so successive tests can't leak overrides into each other.
    lines.append("\t// Reset test seams (each httptest server starts with fresh overrides).")
    lines.append("\t_clock = SystemClock{}")
    if (bc.resilience or {}).get('policies'):
        lines.append("\t_forcedCircuitStates = map[string]string{}")
    lines.append("\t_stores = map[string]any{}")
    if is_pg:
        # Lazy-init the package-level pool from DB_CONNECTION_STRING. Shared
        # across buildHandler() invocations so tests can re-call buildHandler
        # without exhausting connections; main() leaks it (process exit).
        lines.append(f'\tif _pgPool == nil {{')
        lines.append(f'\t\tconnStr := os.Getenv("{db_conn_env}")')
        lines.append('\t\tif connStr == "" {')
        lines.append('\t\t\tconnStr = "postgres://postgres:postgres@localhost:5432/ailang"')
        lines.append("\t\t}")
        lines.append("\t\tpool, err := pgxpool.New(context.Background(), connStr)")
        lines.append('\t\tif err != nil { log.Fatalf("pgx pool: %v", err) }')
        lines.append("\t\t_pgPool = pool")
        lines.append("\t}")
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            lines.append(f"\tvar store I{schema}Store = NewPostgres{schema}Store(_pgPool)")
            lines.append(f'\t_stores["{schema}"] = store  // test seam — PG tests reach the store for helpers')
    else:
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            lines.append(f"\tstore := New{schema}Store()")
            lines.append(f'\t_stores["{schema}"] = store  // test seam — Seed() reachable from main_test.go')
    if uses_call_service_anywhere:
        lines.append("\tserviceURLs := map[string]string{")
        for svc_name, svc_def in service_refs_go.items():
            env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
            default_url = svc_def.get('default', 'http://localhost')
            lines.append(f'\t\t"{svc_name}": func() string {{ if v := os.Getenv("{env_var}"); v != "" {{ return v }}; return "{default_url}" }}(),')
        lines.append("\t}")
        lines.append("\t_serviceURLs = serviceURLs")
    if uses_events_go:
        lines.append("\tbus := NewEventBus()")
        lines.append("\t_bus = bus")
        for h_name, h_def in event_handlers_go:
            evt = h_def.get('event') or h_name
            fn_name = pascal_case(h_name) if h_name.startswith('on_') else 'On' + pascal_case(h_name)
            lines.append(f'\tbus.Subscribe("{evt}", {fn_name})')
    router_fields = []
    if bc.stores:
        router_fields.append("store: store")
    if uses_call_service_anywhere:
        router_fields.append("serviceURLs: serviceURLs")
    if uses_events_go:
        router_fields.append("bus: bus")
    lines.append(f"\trouter := &Router{{{', '.join(router_fields)}}}")
    lines.append("\treturn corsMiddleware(tracingMiddleware(router.mux()))")
    lines.append("}")
    lines.append("")

    # Main — always delegates to buildHandler() so PG and in-memory paths stay
    # in sync with tests.
    lines.append("func main() {")
    lines.append(f'\t// JWT env var: {jwt_env}')
    lines.append("\thandler := buildHandler()")
    lines.append("")
    lines.append(f'\tfmt.Println("[SERVICE] {service_name} STARTED on :{port}")')
    lines.append(f'\tfmt.Println("[READY]")')
    lines.append(f'\tlog.Fatal(http.ListenAndServe(":{port}", handler))')
    lines.append("}")

    with open(os.path.join(output_dir, "main.go"), 'w') as f:
        f.write('\n'.join(lines))

    # go.mod — bump to 1.22 for the new ServeMux patterns; add pgx when needed.
    # Test-only deps (testcontainers) go in the same require block — `go mod tidy`
    # would classify them correctly; keeping them here keeps the file stable.
    requires = ['\tgithub.com/google/uuid v1.6.0']
    if is_pg:
        requires.append('\tgithub.com/jackc/pgx/v5 v5.7.5')
    if needs_resilience_go:
        requires.append('\tgithub.com/sony/gobreaker/v2 v2.0.0')
    if is_pg and bc.tests:
        requires.append('\tgithub.com/testcontainers/testcontainers-go v0.34.0')
        requires.append('\tgithub.com/testcontainers/testcontainers-go/modules/postgres v0.34.0')
    go_mod = (
        f"module ailang-{service_name.replace('_', '-')}\n\n"
        f"go 1.25\n\n"
        f"require (\n" + "\n".join(requires) + "\n)\n"
    )
    with open(os.path.join(output_dir, "go.mod"), 'w') as f:
        f.write(go_mod)

    print(f"  ✓ Go project generated in {output_dir}/")


def generate_schema_go(name: str, schema_def: dict) -> list[str]:
    lines = []
    fields = schema_def.get('fields', {})

    lines.append(f"// SCHEMA: {name} (from bytecode)")
    lines.append(f"type {name} struct {{")
    for field_name, field_def in fields.items():
        ftype = field_def['type']
        go_type = TypeMapper.to_go(ftype, field_def.get('of'))
        prop = pascal_case(field_name)
        # Optional response fields use omitzero (Go 1.24+) so zero values round-trip cleanly.
        is_mandatory = field_def.get('auto_gen', False) or field_def.get('required', False)
        tag = f'json:"{field_name}"' if is_mandatory else f'json:"{field_name},omitzero"'
        lines.append(f'\t{prop} {go_type} `{tag}`')
    lines.append("}")
    lines.append("")

    # DTO (request body) — optional fields use omitzero on marshal side.
    dto_fields = {k: v for k, v in fields.items() if not v.get('auto_gen')}
    lines.append(f"type {name}CreateDTO struct {{")
    for field_name, field_def in dto_fields.items():
        ftype = field_def['type']
        go_type = TypeMapper.to_go(ftype, field_def.get('of'))
        prop = pascal_case(field_name)
        is_required = field_def.get('required', False)
        tag = f'json:"{field_name}"' if is_required else f'json:"{field_name},omitzero"'
        lines.append(f'\t{prop} {go_type} `{tag}`')
    lines.append("}")
    lines.append("")

    # Validation
    lines.append(f"func (d {name}CreateDTO) Validate() *AiLangError {{")
    for field_name, field_def in dto_fields.items():
        for line in generate_validation_go(field_name, field_def, "d"):
            lines.append(line)
    lines.append("\treturn nil")
    lines.append("}")

    return lines


def generate_saga_compensators_go(bc) -> list[str]:
    """Emit a free function per saga that logs the compensation plan (Go mirror of C#)."""
    lines = []
    for saga_name, saga_def in (bc.sagas or {}).items():
        fn = 'compensate' + pascal_case(saga_name)
        lines.append(f"// ── SAGA COMPENSATOR: {saga_name} ──")
        lines.append(f"func {fn}(ctx context.Context) {{")
        lines.append(f'\tslog.Warn("saga compensation triggered", "saga", "{saga_name}")')
        for step in reversed(saga_def.get('steps', [])):
            step_name = step['name']
            comp = step.get('compensate', {})
            op = comp.get('op', 'LOG')
            plan = _describe_compensate(op, comp)
            lines.append(f'\tslog.Info("saga step compensated", "saga", "{saga_name}", "step", "{step_name}", "plan", "{plan}")')
        lines.append("}")
        lines.append("")
    return lines


def _emit_event_handler_go(handler_name: str, handler_def: dict, bc) -> list[str]:
    """Emit an EVENT_HANDLER as a free `func(ctx, data any)`.

    Not a method on Router — keeps the Subscribe() registration straightforward
    (`bus.Subscribe("X", OnX)`) and means the handler can't accidentally depend
    on request-scoped state like `r` or `rt`.
    """
    fn_name = pascal_case(handler_name) if handler_name.startswith('on_') else 'On' + pascal_case(handler_name)
    lines = [
        f"// {handler_name} (EVENT_HANDLER for '{handler_def.get('event', '')}')",
        f"func {fn_name}(ctx context.Context, data any) {{",
    ]
    ctx = {
        'is_pg': False,
        'store_def': None,
        'custom_errors': bc.custom_errors,
        'service_refs': {},
        'resilience_policies': {},
        # `data` is raw any (from event bus); register as 'any_json' so dotted
        # paths like data.items get resolved as `data.(map[string]any)["items"]`
        # by _go_resolve_path. Mirrors C#'s `_evtData` of type JsonNode.
        'locals': {'data': 'any_json'},
        # Tells CALL_SERVICE / EMIT_EVENT / SAGA_COMPENSATE codegen to use
        # the package-level `_serviceURLs` / `_bus` and the `ctx` parameter,
        # instead of `rt.serviceURLs` / `rt.bus` / `r.Context()` which only
        # exist inside HTTP handlers with a Router receiver and *http.Request.
        'in_event_handler': True,
    }

    # Rewrite `event.data.X` → `data.X` in instruction trees (mirrors C# which
    # does the same with `_evtData`). AILang's `event.data` IS the payload root,
    # so stripping the `event.` prefix aligns it with our `data` parameter.
    import copy
    def _rewrite_event_refs(inst):
        inst = copy.deepcopy(inst)
        for key in ('collection', 'value', 'assign', 'name', 'target'):
            v = inst.get(key)
            if isinstance(v, str):
                if v.startswith('event.data.'):
                    inst[key] = 'data.' + v[len('event.data.'):]
                elif v == 'event.data':
                    inst[key] = 'data'
        if 'body' in inst and isinstance(inst['body'], list):
            inst['body'] = [_rewrite_event_refs(i) for i in inst['body'] if isinstance(i, dict)]
        if 'body' in inst and isinstance(inst['body'], dict):
            inst['body'] = {k: ('data.' + v[len('event.data.'):] if isinstance(v, str) and v.startswith('event.data.') else v)
                            for k, v in inst['body'].items()}
        if 'cases' in inst and isinstance(inst['cases'], dict):
            inst['cases'] = {k: [_rewrite_event_refs(i) for i in v if isinstance(i, dict)]
                             for k, v in inst['cases'].items()}
        return inst

    for inst in handler_def.get('instructions', []):
        lines.extend(compile_instruction_go(_rewrite_event_refs(inst), 1, ctx))
    lines.append("}")
    return lines


def generate_event_bus_go() -> list[str]:
    """EventBus: subscriber registry + fire-and-forget Publish (goroutine per listener)."""
    return [
        "// ── EVENT BUS (in-process, fire-and-forget) ──",
        "type EventHandler func(ctx context.Context, data any)",
        "",
        "type EventBus struct {",
        "\tmu   sync.RWMutex",
        "\tsubs map[string][]EventHandler",
        "}",
        "",
        "func NewEventBus() *EventBus {",
        "\treturn &EventBus{subs: map[string][]EventHandler{}}",
        "}",
        "",
        "func (b *EventBus) Subscribe(name string, h EventHandler) {",
        "\tb.mu.Lock(); defer b.mu.Unlock()",
        "\tb.subs[name] = append(b.subs[name], h)",
        "}",
        "",
        "func (b *EventBus) Publish(ctx context.Context, name string, data any) {",
        "\tb.mu.RLock()",
        "\thandlers := append([]EventHandler(nil), b.subs[name]...)  // snapshot",
        "\tb.mu.RUnlock()",
        "\tfor _, h := range handlers {",
        "\t\tgo func(fn EventHandler) {",
        "\t\t\tdefer func() {",
        '\t\t\t\tif r := recover(); r != nil { slog.Error("event handler panic", "event", name, "err", r) }',
        "\t\t\t}()",
        "\t\t\tfn(ctx, data)",
        "\t\t}(h)",
        "\t}",
        "}",
    ]


def generate_service_client_go(bc) -> list[str]:
    """Global helper: callService(…) → any.

    Returns `map[string]any` for JSON objects and `[]any` for JSON arrays
    (encoding/json's standard decode into any).  Returns nil on failure.
    Two variants: policy-less (simple HTTP with 10s timeout) and full
    (circuit breaker + retry). Selected by whether the bytecode declares
    resilience policies.
    """
    policies = (bc.resilience or {}).get('policies', {}) or {}
    service_refs = {n: d for n, d in (bc.dependencies or {}).items() if d.get('type') == 'SERVICE_REF'}

    if not policies:
        return [
            "// ── INTER-SERVICE HTTP HELPER (no resilience policies) ──",
            "var _httpClient = &http.Client{Timeout: 10 * time.Second}",
            "",
            "// callService returns (body, nil) on 2xx; (nil, nil) on upstream 4xx (caller maps);",
            "// and (nil, *AiLangError) when the call failed with a known transport error.",
            "func callService(ctx context.Context, svcName, baseURL, method, path string, body any, _idempotent bool) (any, *AiLangError) {",
            "\tvar reqBody io.Reader",
            "\tif body != nil {",
            "\t\tb, err := json.Marshal(body)",
            '\t\tif err != nil { slog.Error("call_service marshal", "err", err); return nil, nil }',
            "\t\treqBody = bytes.NewReader(b)",
            "\t}",
            "\treq, err := http.NewRequestWithContext(ctx, method, baseURL+path, reqBody)",
            '\tif err != nil { slog.Error("call_service req", "err", err); return nil, nil }',
            '\tif body != nil { req.Header.Set("Content-Type", "application/json") }',
            "\tresp, err := _httpClient.Do(req)",
            "\tif err != nil {",
            '\t\tslog.Error("call_service do", "err", err, "svc", svcName)',
            "\t\treturn nil, nil",
            "\t}",
            "\tdefer resp.Body.Close()",
            "\tif resp.StatusCode >= 400 {",
            '\t\tslog.Info("call_service non-2xx", "status", resp.StatusCode, "path", path)',
            "\t\treturn nil, nil",
            "\t}",
            "\tvar out any",
            "\tif err := json.NewDecoder(resp.Body).Decode(&out); err != nil {",
            '\t\tslog.Error("call_service decode", "err", err)',
            "\t\treturn nil, nil",
            "\t}",
            "\treturn out, nil",
            "}",
        ]

    # Full version with CB + retry when policies are declared.
    # Map from svc_name → its policy (so the runtime can look up CB + retry).
    svc_policy = {n: d.get('resilience') for n, d in service_refs.items() if d.get('resilience')}

    lines = [
        "// ── INTER-SERVICE HTTP HELPER ──",
        "var _httpClient = &http.Client{}  // per-attempt timeout handled via ctx.WithTimeout",
        "",
    ]

    # Emit one sync.Once + CB-per-policy.
    if policies:
        lines.append("// Circuit breakers (one per resilience policy), lazily initialized.")
        lines.append("var _cbs = map[string]*gobreaker.CircuitBreaker[[]byte]{}")
        lines.append("var _cbsOnce sync.Once")
        lines.append("")
        lines.append("func _initCBs() {")
        for pname, pol in policies.items():
            cb = pol.get('circuit_breaker') or {}
            ft = cb.get('failure_threshold', 5)
            rt_ms = cb.get('reset_timeout_ms', 30000)
            lines.append(f'\t_cbs["{pname}"] = gobreaker.NewCircuitBreaker[[]byte](gobreaker.Settings{{')
            lines.append(f'\t\tName:        "{pname}",')
            lines.append(f'\t\tMaxRequests: 1,  // half-open probes')
            lines.append(f'\t\tInterval:    0,  // don\'t reset counts while closed')
            lines.append(f'\t\tTimeout:     {rt_ms} * time.Millisecond,  // how long to stay open')
            lines.append(f'\t\tReadyToTrip: func(c gobreaker.Counts) bool {{ return c.ConsecutiveFailures >= {ft} }},')
            lines.append(f'\t\tOnStateChange: func(name string, from, to gobreaker.State) {{')
            lines.append(f'\t\t\tslog.Info("circuit_breaker_state", "policy", name, "from", from.String(), "to", to.String())')
            lines.append(f'\t\t}},')
            lines.append("\t})")
        lines.append("}")
        lines.append("")

    # Per-policy retry settings baked into a lookup — the helper reads them at call time.
    if policies:
        lines.append("// Retry settings indexed by policy name.")
        lines.append("type _retryCfg struct { MaxAttempts int; InitialMs, MaxMs int; Exponential bool }")
        lines.append("var _retrySettings = map[string]_retryCfg{")
        for pname, pol in policies.items():
            r = pol.get('retry') or {}
            if not r:
                continue
            max_a = r.get('max_attempts', 3)
            initial = r.get('initial_ms', 100)
            max_ms = r.get('max_ms', 2000)
            expo = 'true' if r.get('backoff_type') == 'exponential' else 'false'
            lines.append(f'\t"{pname}": {{MaxAttempts: {max_a}, InitialMs: {initial}, MaxMs: {max_ms}, Exponential: {expo}}},')
        lines.append("}")
        lines.append("")

    # Per-policy timeout settings.
    if policies:
        lines.append("var _timeouts = map[string]time.Duration{")
        for pname, pol in policies.items():
            t = (pol.get('timeout') or {}).get('duration_ms', 10000)
            lines.append(f'\t"{pname}": {t} * time.Millisecond,')
        lines.append("}")
        lines.append("")

    # Svc → policy lookup baked in so callService can map svc→policy without extra arg.
    if svc_policy:
        lines.append("var _svcPolicies = map[string]string{")
        for svc, pol in svc_policy.items():
            lines.append(f'\t"{svc}": "{pol}",')
        lines.append("}")
        lines.append("")

    lines.extend([
        "// callService performs a single HTTP call with (optional) circuit breaker,",
        "// per-attempt timeout, and retry-with-backoff.",
        "// Returns (body, nil) on success, (nil, nil) on generic failure or upstream 4xx,",
        "// and (nil, ErrTimeout|ErrUnavailable) when a known transport error occurred.",
        "func callService(ctx context.Context, svcName, baseURL, method, path string, body any, idempotent bool) (any, *AiLangError) {",
        "\tif len(_cbs) > 0 { _cbsOnce.Do(_initCBs) }",
        '\tpolicy := _svcPolicies[svcName]',
        "\t// Test seam: force the breaker open without tripping it via failures.",
        '\tif _forcedCircuitStates[policy] == "OPEN" {',
        "\t\treturn nil, ErrUnavailable(svcName)",
        "\t}",
        "\ttimeout := 10 * time.Second",
        "\tif t, ok := _timeouts[policy]; ok { timeout = t }",
        "\tmaxAttempts := 1",
        "\tinitial := 0 * time.Millisecond",
        "\tmaxBackoff := 0 * time.Millisecond",
        "\texpo := false",
        "\tif idempotent {",
        "\t\tif cfg, ok := _retrySettings[policy]; ok {",
        "\t\t\tmaxAttempts = cfg.MaxAttempts + 1  // +1 because the first try isn't a retry",
        "\t\t\tinitial = time.Duration(cfg.InitialMs) * time.Millisecond",
        "\t\t\tmaxBackoff = time.Duration(cfg.MaxMs) * time.Millisecond",
        "\t\t\texpo = cfg.Exponential",
        "\t\t}",
        "\t}",
        "\tcb := _cbs[policy]",
        "",
        "\tvar lastBody []byte",
        "\tvar lastErr error",
        "\tfor attempt := 0; attempt < maxAttempts; attempt++ {",
        "\t\tif attempt > 0 {",
        "\t\t\tdelay := initial",
        "\t\t\tif expo { delay = initial << (attempt - 1) }",
        "\t\t\tif maxBackoff > 0 && delay > maxBackoff { delay = maxBackoff }",
        "\t\t\tselect {",
        "\t\t\tcase <-time.After(delay):",
        "\t\t\tcase <-ctx.Done():",
        "\t\t\t\treturn nil, nil",
        "\t\t\t}",
        "\t\t}",
        "\t\tattemptCtx, cancel := context.WithTimeout(ctx, timeout)",
        "\t\tattemptFn := func() ([]byte, error) {",
        "\t\t\treturn _doHTTP(attemptCtx, baseURL, method, path, body)",
        "\t\t}",
        "\t\tvar (b []byte; err error)",
        "\t\tif cb != nil {",
        "\t\t\tb, err = cb.Execute(attemptFn)",
        "\t\t} else {",
        "\t\t\tb, err = attemptFn()",
        "\t\t}",
        "\t\tcancel()",
        "\t\tif err == nil {",
        "\t\t\tlastBody = b",
        "\t\t\tlastErr = nil",
        "\t\t\tbreak  // success",
        "\t\t}",
        "\t\tlastErr = err",
        '\t\tslog.Info("call_service attempt failed", "svc", svcName, "attempt", attempt+1, "err", err.Error())',
        "\t}",
        "\tif lastErr != nil {",
        "\t\t// gobreaker returns ErrOpenState when the breaker is open.",
        "\t\tif errors.Is(lastErr, gobreaker.ErrOpenState) || errors.Is(lastErr, gobreaker.ErrTooManyRequests) {",
        "\t\t\treturn nil, ErrUnavailable(svcName)",
        "\t\t}",
        "\t\t// Per-attempt ctx.WithTimeout expiry surfaces as DeadlineExceeded.",
        '\t\tif errors.Is(lastErr, context.DeadlineExceeded) || strings.Contains(lastErr.Error(), "context deadline exceeded") {',
        "\t\t\treturn nil, ErrTimeout(svcName)",
        "\t\t}",
        "\t\treturn nil, nil",
        "\t}",
        "\tif lastBody == nil { return nil, nil }",
        "\tvar out any",
        "\tif err := json.Unmarshal(lastBody, &out); err != nil {",
        '\t\tslog.Error("call_service decode", "err", err)',
        "\t\treturn nil, nil",
        "\t}",
        "\treturn out, nil",
        "}",
        "",
        "// _doHTTP is a single HTTP attempt, no retry/CB — those are wrapped by callService.",
        "func _doHTTP(ctx context.Context, baseURL, method, path string, body any) ([]byte, error) {",
        "\tvar reqBody io.Reader",
        "\tif body != nil {",
        "\t\tb, err := json.Marshal(body)",
        "\t\tif err != nil { return nil, err }",
        "\t\treqBody = bytes.NewReader(b)",
        "\t}",
        "\treq, err := http.NewRequestWithContext(ctx, method, baseURL+path, reqBody)",
        "\tif err != nil { return nil, err }",
        '\tif body != nil { req.Header.Set("Content-Type", "application/json") }',
        "\tresp, err := _httpClient.Do(req)",
        "\tif err != nil { return nil, err }",
        "\tdefer resp.Body.Close()",
        "\tif resp.StatusCode >= 500 {",
        '\t\treturn nil, fmt.Errorf("upstream 5xx: %d", resp.StatusCode)',
        "\t}",
        "\tif resp.StatusCode >= 400 {",
        "\t\treturn nil, nil  // 4xx is not retryable — return empty body, no error",
        "\t}",
        "\treturn io.ReadAll(resp.Body)",
        "}",
    ])
    return lines


def generate_paginated_go() -> list[str]:
    """Emit the shared Paginated[T] generic struct (Go 1.18+)."""
    return [
        "// SHARED TYPE: Paginated[T] (Go generics)",
        "type Paginated[T any] struct {",
        '\tData       []T   `json:"data"`',
        '\tPage       int   `json:"page"`',
        '\tLimit      int   `json:"limit"`',
        '\tTotal      int64 `json:"total"`',
        '\tTotalPages int   `json:"total_pages"`',
        "}",
    ]


def generate_postgres_store_go(store_name: str, store_def: dict, schema_def: dict) -> list[str]:
    """Generate a PostgreSQL-backed store in Go using pgxpool + pgx.NamedArgs.

    Emits:
      - I<Schema>Store interface (methods take ctx and return (T, error))
      - Postgres<Schema>Store struct with *pgxpool.Pool
      - scan<Schema>(pgx.Row) helper for mapping a row to the struct
    """
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    mapping = list(store_def.get('mapping', {}).items())
    if not mapping:
        return [f"// WARN: store {store_name} has no (mapping ...) — skipping PG generation"]

    # Partition fields like the C# side: db-managed (auto_gen or schema default)
    # stay out of the INSERT so column DEFAULTs apply; writable fields come from
    # the DTO. Kept in sync so Go and C# produce the same rows for the same body.
    def _is_db_managed(field_name: str) -> bool:
        sfield = schema_def.get('fields', {}).get(field_name, {})
        return bool(sfield.get('auto_gen')) or sfield.get('default') is not None
    auto_fields = [(f, m) for f, m in mapping if _is_db_managed(f)]
    writable_fields = [(f, m) for f, m in mapping if not _is_db_managed(f)]

    all_columns_sql = ", ".join(m['column'] for _, m in mapping)
    writable_column_sql = ", ".join(m['column'] for _, m in writable_fields)
    writable_placeholders = ", ".join(f"@{f}" for f, _ in writable_fields)

    insert_sql = (
        f"INSERT INTO {table} ({writable_column_sql})\n"
        f"VALUES ({writable_placeholders})\n"
        f"RETURNING {all_columns_sql}"
    )
    set_clauses = [f"{m['column']}=@{f}" for f, m in writable_fields]
    if 'updated_at' in dict(mapping):
        set_clauses.append("updated_at=NOW()")
    update_sql = (
        f"UPDATE {table}\n"
        f"SET {', '.join(set_clauses)}\n"
        f"WHERE id=@id\n"
        f"RETURNING {all_columns_sql}"
    )
    select_by_id_sql = f"SELECT {all_columns_sql}\nFROM {table}\nWHERE id=@id"
    select_all_sql = (
        f"SELECT {all_columns_sql}\n"
        f"FROM {table}\n"
        f"ORDER BY id\n"
        f"LIMIT @_limit OFFSET @_offset"
    )
    count_all_sql = f"SELECT COUNT(*) FROM {table}"
    delete_sql = f"DELETE FROM {table} WHERE id=@id"

    # Scan destinations & struct-field assignments (indexed so scanRow pre-allocs in order)
    scan_dests = []
    struct_fields = []
    for field, m in mapping:
        prop = pascal_case(field)
        struct_fields.append(prop)
        scan_dests.append(f"&p.{prop}")

    lines = []
    lines.append(f"// STORE: Postgres{schema}Store (from bytecode mapping)")
    lines.append(f"type I{schema}Store interface {{")
    lines.append("\tPool() *pgxpool.Pool")
    lines.append(f"\tListAll(ctx context.Context, page, limit, maxLimit int) (Paginated[{schema}], error)")
    lines.append(f"\tFind(ctx context.Context, id string) (*{schema}, error)")
    lines.append(f"\tInsert(ctx context.Context, data {schema}CreateDTO) ({schema}, error)")
    lines.append(f"\tUpdate(ctx context.Context, id string, data {schema}CreateDTO) (*{schema}, error)")
    lines.append("\tDelete(ctx context.Context, id string) (bool, error)")
    lines.append("}")
    lines.append("")

    lines.append(f"type Postgres{schema}Store struct {{")
    lines.append("\tpool *pgxpool.Pool")
    lines.append("}")
    lines.append("")
    lines.append(f"func NewPostgres{schema}Store(pool *pgxpool.Pool) *Postgres{schema}Store {{")
    lines.append(f"\treturn &Postgres{schema}Store{{pool: pool}}")
    lines.append("}")
    lines.append("")
    lines.append(f"func (s *Postgres{schema}Store) Pool() *pgxpool.Pool {{ return s.pool }}")
    lines.append("")

    # scan helper (works with both pgx.Row and pgx.Rows)
    lines.append(f"// scan{schema} scans a row (either pgx.Row from QueryRow or pgx.Rows in Next loop).")
    lines.append(f"func scan{schema}(row interface{{ Scan(dest ...any) error }}) ({schema}, error) {{")
    lines.append(f"\tvar p {schema}")
    lines.append(f"\terr := row.Scan({', '.join(scan_dests)})")
    lines.append(f"\treturn p, err")
    lines.append("}")
    lines.append("")

    # ListAll (paginated)
    lines.append(f"func (s *Postgres{schema}Store) ListAll(ctx context.Context, page, limit, maxLimit int) (Paginated[{schema}], error) {{")
    lines.append("\tif maxLimit < 1 { maxLimit = 100 }")
    lines.append("\tif limit < 1 { limit = 20 }")
    lines.append("\tif limit > maxLimit { limit = maxLimit }")
    lines.append("\tif page < 1 { page = 1 }")
    lines.append("\toffset := (page - 1) * limit")
    lines.append("\tvar total int64")
    lines.append(f'\tif err := s.pool.QueryRow(ctx, {emit_sql_literal_go(count_all_sql)}).Scan(&total); err != nil {{')
    lines.append(f"\t\treturn Paginated[{schema}]{{}}, err")
    lines.append("\t}")
    lines.append(f'\trows, err := s.pool.Query(ctx, {emit_sql_literal_go(select_all_sql)}, pgx.NamedArgs{{"_limit": limit, "_offset": offset}})')
    lines.append("\tif err != nil {")
    lines.append(f"\t\treturn Paginated[{schema}]{{}}, err")
    lines.append("\t}")
    lines.append("\tdefer rows.Close()")
    lines.append(f"\titems := make([]{schema}, 0)")
    lines.append("\tfor rows.Next() {")
    lines.append(f"\t\tp, err := scan{schema}(rows)")
    lines.append("\t\tif err != nil {")
    lines.append(f"\t\t\treturn Paginated[{schema}]{{}}, err")
    lines.append("\t\t}")
    lines.append("\t\titems = append(items, p)")
    lines.append("\t}")
    lines.append("\ttotalPages := 0")
    lines.append("\tif limit > 0 {")
    lines.append("\t\ttotalPages = int((total + int64(limit) - 1) / int64(limit))")
    lines.append("\t}")
    lines.append(f"\treturn Paginated[{schema}]{{Data: items, Page: page, Limit: limit, Total: total, TotalPages: totalPages}}, nil")
    lines.append("}")
    lines.append("")

    # Find
    lines.append(f"func (s *Postgres{schema}Store) Find(ctx context.Context, id string) (*{schema}, error) {{")
    lines.append(f'\trow := s.pool.QueryRow(ctx, {emit_sql_literal_go(select_by_id_sql)}, pgx.NamedArgs{{"id": id}})')
    lines.append(f"\tp, err := scan{schema}(row)")
    lines.append("\tif err != nil {")
    lines.append("\t\tif errors.Is(err, pgx.ErrNoRows) { return nil, nil }")
    lines.append("\t\treturn nil, err")
    lines.append("\t}")
    lines.append("\treturn &p, nil")
    lines.append("}")
    lines.append("")

    # Insert
    lines.append(f"func (s *Postgres{schema}Store) Insert(ctx context.Context, data {schema}CreateDTO) ({schema}, error) {{")
    lines.append("\targs := pgx.NamedArgs{")
    for f, _m in writable_fields:
        prop = pascal_case(f)
        lines.append(f'\t\t"{f}": data.{prop},')
    lines.append("\t}")
    lines.append(f'\trow := s.pool.QueryRow(ctx, {emit_sql_literal_go(insert_sql)}, args)')
    lines.append(f"\treturn scan{schema}(row)")
    lines.append("}")
    lines.append("")

    # Update
    lines.append(f"func (s *Postgres{schema}Store) Update(ctx context.Context, id string, data {schema}CreateDTO) (*{schema}, error) {{")
    lines.append("\targs := pgx.NamedArgs{\"id\": id,")
    for f, _m in writable_fields:
        prop = pascal_case(f)
        lines.append(f'\t\t"{f}": data.{prop},')
    lines.append("\t}")
    lines.append(f'\trow := s.pool.QueryRow(ctx, {emit_sql_literal_go(update_sql)}, args)')
    lines.append(f"\tp, err := scan{schema}(row)")
    lines.append("\tif err != nil {")
    lines.append("\t\tif errors.Is(err, pgx.ErrNoRows) { return nil, nil }")
    lines.append("\t\treturn nil, err")
    lines.append("\t}")
    lines.append("\treturn &p, nil")
    lines.append("}")
    lines.append("")

    # Delete
    lines.append(f"func (s *Postgres{schema}Store) Delete(ctx context.Context, id string) (bool, error) {{")
    lines.append(f'\ttag, err := s.pool.Exec(ctx, {emit_sql_literal_go(delete_sql)}, pgx.NamedArgs{{"id": id}})')
    lines.append("\tif err != nil { return false, err }")
    lines.append("\treturn tag.RowsAffected() > 0, nil")
    lines.append("}")
    return lines


def generate_row_mappers_go(bc) -> list[str]:
    """Emit readRow<Schema>(rows) helpers for schemas targeted by SQL_QUERY/SQL_STREAM."""
    lines = []
    targets = set()
    for h in bc.handlers.values():
        for inst in h.get('instructions', []):
            if inst.get('op') in ('SQL_QUERY', 'SQL_STREAM'):
                t = (h.get('returns') or {}).get('type')
                if t:
                    targets.add(t)
    for tname in targets:
        sd = bc.schemas.get(tname)
        if not sd:
            continue
        lines.append(f"// ROW MAPPER: readRow{tname} (by column name, null-safe)")
        lines.append(f"func readRow{tname}(rows pgx.Rows) ({tname}, error) {{")
        lines.append(f"\tvar out {tname}")
        lines.append("\tvals, err := rows.Values()")
        lines.append("\tif err != nil { return out, err }")
        lines.append("\tfds := rows.FieldDescriptions()")
        lines.append("\tfor i, fd := range fds {")
        lines.append("\t\tswitch string(fd.Name) {")
        for field_name, field_def in sd.get('fields', {}).items():
            prop = pascal_case(field_name)
            ftype = field_def.get('type')
            go_type = TypeMapper.to_go(ftype)
            lines.append(f'\t\tcase "{field_name}":')
            if go_type == 'string':
                lines.append(f"\t\t\tif v, ok := vals[i].(string); ok {{ out.{prop} = v }}")
            elif go_type == 'int':
                lines.append(f"\t\t\tswitch v := vals[i].(type) {{")
                lines.append(f"\t\t\tcase int:   out.{prop} = v")
                lines.append(f"\t\t\tcase int32: out.{prop} = int(v)")
                lines.append(f"\t\t\tcase int64: out.{prop} = int(v)")
                lines.append(f"\t\t\t}}")
            elif go_type == 'float64':
                lines.append(f"\t\t\tswitch v := vals[i].(type) {{")
                lines.append(f"\t\t\tcase float64: out.{prop} = v")
                lines.append(f"\t\t\tcase float32: out.{prop} = float64(v)")
                lines.append(f"\t\t\t}}")
            elif go_type == 'bool':
                lines.append(f"\t\t\tif v, ok := vals[i].(bool); ok {{ out.{prop} = v }}")
            elif go_type == 'time.Time':
                lines.append(f"\t\t\tif v, ok := vals[i].(time.Time); ok {{ out.{prop} = v }}")
            else:
                lines.append(f"\t\t\t// {prop}: unhandled type {go_type}")
        lines.append("\t\t}")
        lines.append("\t}")
        lines.append("\treturn out, nil")
        lines.append("}")
        lines.append("")
    return lines


def generate_store_go(schema: str, schema_def: dict, store_def: dict = None) -> list[str]:
    lines = []
    fields = schema_def.get('fields', {})
    # Custom primary key support: resolve from store.index, fallback to 'id'.
    index = (store_def or {}).get('index') or {}
    pk_field = index.get('field', 'id') if isinstance(index, dict) else 'id'
    pk_fdef = fields.get(pk_field, {})
    pk_type_ailang = pk_fdef.get('type', 'uuid')
    pk_go_type = ailang_type_to_go(pk_type_ailang) if pk_type_ailang else 'string'
    pk_prop = pascal_case(pk_field)
    pk_is_auto = pk_fdef.get('auto_gen', False)
    # Map-key type: stringifiable primitives only. int/decimal/bool/uuid/string
    # all round-trip through string via fmt.Sprint; for others fall back to string.
    pk_map_key = pk_go_type if pk_go_type in ('string', 'int', 'int64') else 'string'
    # DTO fields exclude auto_gen ones. When pk is auto-gen, also exclude it
    # from the DTO Insert/Update shape.
    dto_fields = {k: v for k, v in fields.items() if not v.get('auto_gen')}

    lines.append(f"// STORE: {schema}Store (from bytecode)")
    lines.append(f"type {schema}Store struct {{")
    lines.append(f"\tmu   sync.RWMutex")
    lines.append(f"\tdata map[{pk_map_key}]{schema}")
    lines.append("}")
    lines.append("")
    lines.append(f"func New{schema}Store() *{schema}Store {{")
    lines.append(f"\treturn &{schema}Store{{data: make(map[{pk_map_key}]{schema})}}")
    lines.append("}")
    lines.append("")

    # ListAll
    lines.append(f"func (s *{schema}Store) ListAll() []{schema} {{")
    lines.append("\ts.mu.RLock(); defer s.mu.RUnlock()")
    lines.append(f"\titems := make([]{schema}, 0, len(s.data))")
    lines.append(f"\tfor _, p := range s.data {{ items = append(items, p) }}")
    lines.append("\treturn items")
    lines.append("}")
    lines.append("")

    # Find
    lines.append(f"func (s *{schema}Store) Find({pk_field} {pk_map_key}) *{schema} {{")
    lines.append("\ts.mu.RLock(); defer s.mu.RUnlock()")
    lines.append(f"\tp, ok := s.data[{pk_field}]")
    lines.append("\tif !ok { return nil }")
    lines.append("\treturn &p")
    lines.append("}")
    lines.append("")

    # Insert — when PK is auto_gen, generate a new uuid; otherwise use dto.<Pk>.
    lines.append(f"func (s *{schema}Store) Insert(dto {schema}CreateDTO) {schema} {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\tp := {schema}{{")
    if pk_is_auto:
        lines.append(f'\t\t{pk_prop}: uuid.New().String(),')
    for field_name in dto_fields:
        prop = pascal_case(field_name)
        lines.append(f"\t\t{prop}: dto.{prop},")
    lines.append("\t}")
    lines.append(f"\ts.data[p.{pk_prop}] = p")
    lines.append("\treturn p")
    lines.append("}")
    lines.append("")

    # Update
    lines.append(f"func (s *{schema}Store) Update({pk_field} {pk_map_key}, dto {schema}CreateDTO) *{schema} {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\tif _, ok := s.data[{pk_field}]; !ok {{ return nil }}")
    lines.append(f"\tp := {schema}{{{pk_prop}: {pk_field},")
    for field_name in dto_fields:
        if field_name == pk_field:
            continue
        prop = pascal_case(field_name)
        lines.append(f"\t\t{prop}: dto.{prop},")
    lines.append("\t}")
    lines.append(f"\ts.data[{pk_field}] = p")
    lines.append("\treturn &p")
    lines.append("}")
    lines.append("")

    # UpdateFields — partial update via a map[string]any of field → new value.
    # Handlers use this for STORE_UPDATE_FIELDS; mirrors C#'s `with { }` clone.
    lines.append(f"func (s *{schema}Store) UpdateFields({pk_field} {pk_map_key}, fields map[string]any) *{schema} {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\texisting, ok := s.data[{pk_field}]")
    lines.append("\tif !ok { return nil }")
    lines.append("\tupdated := existing")
    for field_name, fdef in fields.items():
        if field_name == pk_field:
            continue
        prop = pascal_case(field_name)
        ftype = fdef.get('type')
        if ftype == 'int':
            cast = 'int(v.(float64))'
        elif ftype == 'decimal':
            cast = 'v.(float64)'
        elif ftype == 'bool':
            cast = 'v.(bool)'
        elif ftype in ('uuid', 'string', 'enum'):
            cast = 'v.(string)'
        elif ftype == 'timestamp':
            cast = 'v.(time.Time)'
        elif ftype in ('date', 'time'):
            cast = 'v.(string)'
        elif ftype == 'list':
            elem = fdef.get('of') or 'any'
            cast = f'v.([]{ailang_type_to_go(elem)})'
        else:
            go_type = ailang_type_to_go(ftype) if ftype else 'any'
            cast = f'v.({go_type})'
        lines.append(f'\tif v, ok := fields["{field_name}"]; ok {{ updated.{prop} = {cast} }}')
    lines.append(f"\ts.data[{pk_field}] = updated")
    lines.append("\treturn &updated")
    lines.append("}")
    lines.append("")

    # Delete
    lines.append(f"func (s *{schema}Store) Delete({pk_field} {pk_map_key}) bool {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\tif _, ok := s.data[{pk_field}]; !ok {{ return false }}")
    lines.append(f"\tdelete(s.data, {pk_field})")
    lines.append("\treturn true")
    lines.append("}")
    lines.append("")

    # Reset — test seam for RESET_STORE in (tests ... (clean ...))
    lines.append(f"func (s *{schema}Store) Reset() {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\ts.data = make(map[{pk_map_key}]{schema})")
    lines.append("}")
    lines.append("")

    # Seed — test seam for (setup (seed_data …)). Inserts at the declared PK
    # without running the Insert auto-gen path. Replaces on conflict.
    lines.append(f"func (s *{schema}Store) Seed(items []{schema}) {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append("\tfor _, item := range items {")
    lines.append(f"\t\ts.data[item.{pk_prop}] = item")
    lines.append("\t}")
    lines.append("}")

    return lines


def generate_error_model_go(custom_errors: dict = None) -> list[str]:
    lines = []
    lines.append("// ERROR MODEL: AILang native errors")
    lines.append("type AiLangError struct { Error ErrorDetail `json:\"error\"` }")
    lines.append("type ErrorDetail struct {")
    lines.append('\tType    string `json:"type"`')
    lines.append('\tMessage string `json:"message"`')
    lines.append('\tEntity  string `json:"entity,omitzero"`')
    lines.append('\tID      string `json:"id,omitzero"`')
    lines.append('\tField   string `json:"field,omitzero"`')
    lines.append('\tRule    string `json:"rule,omitzero"`')
    lines.append("}")
    lines.append("")

    lines.append("func (e *AiLangError) StatusCode() int {")
    lines.append('\tswitch e.Error.Type {')
    lines.append('\tcase "NOT_FOUND": return 404')
    lines.append('\tcase "VALIDATION", "INVALID_BODY", "INVALID_PARAM": return 400')
    lines.append('\tcase "BUSINESS_RULE": return 422')
    lines.append('\tcase "UNAUTHORIZED": return 401')
    lines.append('\tcase "FORBIDDEN": return 403')
    lines.append('\tcase "UNAVAILABLE": return 503')
    lines.append('\tcase "TIMEOUT": return 504')
    lines.append('\tcase "ALREADY_EXISTS", "CONFLICT": return 409')
    # Custom errors — mapped to their declared http_status.
    by_status: dict[int, list[str]] = {}
    for err_type, entry in (custom_errors or {}).items():
        tag = err_type_tag(err_type)
        by_status.setdefault(entry.get('http_status', 422), []).append(tag)
    for status, tags in by_status.items():
        cases = ", ".join(f'"{t}"' for t in tags)
        lines.append(f'\tcase {cases}: return {status}')
    lines.append('\tdefault: return 500')
    lines.append("\t}")
    lines.append("}")
    lines.append("")

    # Error constructors
    constructors = [
        ("ErrNotFound", "entity, id string", 'NOT_FOUND', 'Entity: entity, ID: id, Message: fmt.Sprintf("%s with id %s not found", entity, id)'),
        ("ErrValidation", "field, reason string", 'VALIDATION', 'Field: field, Message: fmt.Sprintf("validation failed: %s — %s", field, reason)'),
        ("ErrBusinessRule", "rule, detail string", 'BUSINESS_RULE', 'Rule: rule, Message: detail'),
        ("ErrUnauthorized", "", 'UNAUTHORIZED', 'Message: "authentication required"'),
        ("ErrInvalidBody", "schema, detail string", 'INVALID_BODY', 'Message: fmt.Sprintf("body does not match schema %s: %s", schema, detail)'),
        ("ErrInvalidParam", "name, expected string", 'INVALID_PARAM', 'Field: name, Message: fmt.Sprintf("parameter %s must be %s", name, expected)'),
        ("ErrInternal", "detail string", 'INTERNAL', 'Message: fmt.Sprintf("internal error: %s", detail)'),
        ("ErrTimeout", "service string", 'TIMEOUT', 'Message: fmt.Sprintf("service %s timed out", service)'),
        ("ErrUnavailable", "service string", 'UNAVAILABLE', 'Message: fmt.Sprintf("service %s is unavailable", service)'),
    ]

    for fname, params, etype, init in constructors:
        param_str = f"({params})" if params else "()"
        lines.append(f"func {fname}{param_str} *AiLangError {{")
        lines.append(f'\treturn &AiLangError{{Error: ErrorDetail{{Type: "{etype}", {init}}}}}')
        lines.append("}")
        lines.append("")

    # Custom error constructors — one per definition in the bytecode.
    for err_type, entry in (custom_errors or {}).items():
        fname = 'Err' + err_name_to_factory(err_type)
        tag = err_type_tag(err_type)
        declared = entry.get('params', []) or []
        template = entry.get('message_template', tag)
        param_camel = {p: camel_case(p) for p in declared}
        sig = ", ".join(f"{param_camel[p]} any" for p in declared)
        fmt_str, arg_order = template_to_go_sprintf(template, param_camel)
        if arg_order:
            args = ", " + ", ".join(arg_order)
            msg_expr = f'fmt.Sprintf("{fmt_str}"{args})'
        else:
            msg_expr = f'"{fmt_str}"'
        lines.append(f"func {fname}({sig}) *AiLangError {{")
        lines.append(f'\treturn &AiLangError{{Error: ErrorDetail{{Type: "{tag}", Message: {msg_expr}}}}}')
        lines.append("}")
        lines.append("")

    return lines


def generate_handler_go(name: str, handler_def: dict, bc, is_pg: bool = False, primary_store_def: dict = None) -> list[str]:
    """Generate a Go handler function."""
    lines = []
    auth = handler_def.get('auth')
    go_name = camel_case(name)
    instructions = handler_def.get('instructions', [])

    lines.append(f"// {name} ({'PUBLIC' if auth == 'PUBLIC' else 'REQUIRES auth'})")
    lines.append(f"func (rt *Router) {go_name}(w http.ResponseWriter, r *http.Request) {{")

    if is_pg:
        lines.append("\tctx := r.Context()")
        lines.append("\t_ = ctx  // ctx used when a PG call appears")
        lines.append("")

    if auth != 'PUBLIC':
        lines.append("\tif !isAuthenticated(r) {")
        lines.append("\t\trespondError(w, ErrUnauthorized())")
        lines.append("\t\treturn")
        lines.append("\t}")
        lines.append("")

    ctx = {
        'is_pg': is_pg,
        'store_def': primary_store_def,
        'returns': handler_def.get('returns'),
        'store_accessor': 'rt.store',
        'custom_errors': bc.custom_errors if bc else {},
        'schemas': bc.schemas if bc else {},  # For Phase 3b CONSTRUCT hints
    }
    for inst in instructions:
        lines.extend(compile_instruction_go(inst, 1, ctx))

    lines.append("}")
    return lines


def compile_instruction_go(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    """Compile a single AILang instruction to Go.

    ctx carries per-handler state:
      - 'is_pg':    bool — store uses PostgreSQL
      - 'store_def':dict — the store definition
      - 'returns':  dict — handler return-type info
      - 'store_accessor': str — expression to reach the store, e.g. 'rt.store'
    """
    pad = "\t" * indent
    op = inst.get('op')
    lines = []
    ctx = ctx or {}
    is_pg = ctx.get('is_pg', False)
    store = ctx.get('store_accessor', 'rt.store')

    if op == 'COMMENT':
        lines.append(f"{pad}// {inst['text']}")

    elif op == 'READ_PARAM':
        # For typed params, the ServeMux path uses `{<name>__raw}`; read via r.PathValue.
        name = inst['name']
        assign = inst['assign']
        ptype = inst.get('type', 'string')
        if ptype == 'uuid':
            lines.append(f'{pad}{assign}_raw := r.PathValue("{name}__raw")')
            lines.append(f'{pad}if _, err := uuid.Parse({assign}_raw); err != nil {{')
            lines.append(f'{pad}\trespondError(w, ErrInvalidParam("{name}", "uuid"))')
            lines.append(f"{pad}\treturn")
            lines.append(f"{pad}}}")
            lines.append(f"{pad}{assign} := {assign}_raw")
        elif ptype == 'int':
            lines.append(f'{pad}{assign}_raw := r.PathValue("{name}__raw")')
            lines.append(f'{pad}{assign}, _err_{assign} := strconv.Atoi({assign}_raw)')
            lines.append(f'{pad}if _err_{assign} != nil {{')
            lines.append(f'{pad}\trespondError(w, ErrInvalidParam("{name}", "int"))')
            lines.append(f"{pad}\treturn")
            lines.append(f"{pad}}}")
        else:
            lines.append(f'{pad}{assign} := r.PathValue("{name}")')

    elif op == 'READ_QUERY':
        name = inst['name']
        ptype = inst.get('type', 'string')
        assign = inst['assign']
        default = inst.get('default')
        if ptype == 'int':
            dflt = int(default) if default is not None else 0
            lines.append(f'{pad}{assign} := {dflt}')
            lines.append(f'{pad}if _q := r.URL.Query().Get("{name}"); _q != "" {{')
            lines.append(f'{pad}\tif _v, _err := strconv.Atoi(_q); _err == nil {{ {assign} = _v }}')
            lines.append(f"{pad}}}")
        elif ptype == 'bool':
            dflt = 'true' if default in (True, 'true') else 'false'
            lines.append(f'{pad}{assign} := {dflt}')
            lines.append(f'{pad}if _q := r.URL.Query().Get("{name}"); _q != "" {{')
            lines.append(f'{pad}\tif _v, _err := strconv.ParseBool(_q); _err == nil {{ {assign} = _v }}')
            lines.append(f"{pad}}}")
        else:
            dflt = f'"{default}"' if default is not None else '""'
            lines.append(f'{pad}{assign} := {dflt}')
            lines.append(f'{pad}if _q := r.URL.Query().Get("{name}"); _q != "" {{ {assign} = _q }}')

    elif op == 'READ_BODY':
        assign = inst['assign']
        schema = inst['schema']
        lines.append(f"{pad}var {assign} {schema}CreateDTO")
        lines.append(f"{pad}if err := json.NewDecoder(r.Body).Decode(&{assign}); err != nil {{")
        lines.append(f'{pad}\trespondError(w, ErrInvalidBody("{schema}", "invalid JSON"))')
        lines.append(f"{pad}\treturn")
        lines.append(f"{pad}}}")
        # Enforce the AILang contract: schema validations run automatically after READ_BODY.
        lines.append(f"{pad}if _verr := {assign}.Validate(); _verr != nil {{ respondError(w, _verr); return }}")
        # Register as the base schema so BRANCH field-type lookups hit
        # `schemas[schema]`. Track DTO-ness separately so STORE_INSERT
        # pass-through (which requires the full entity) doesn't misfire.
        ctx.setdefault('locals', {})[assign] = schema
        ctx.setdefault('_dto_vars', set()).add(assign)

    elif op == 'STORE_LIST_ALL':
        assign = inst['assign']
        paginate = inst.get('paginate')
        if is_pg and paginate:
            page = paginate.get('page') or 1
            limit = paginate.get('limit') or 20
            max_limit = paginate.get('max_limit') or 100
            lines.append(f"{pad}{assign}, err := {store}.ListAll(ctx, {page}, {limit}, {max_limit})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        elif is_pg:
            lines.append(f"{pad}{assign}, err := {store}.ListAll(ctx, 1, 100, 100)")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{assign} := {store}.ListAll()")

    elif op == 'STORE_FIND':
        assign = inst['assign']
        store_schema = (ctx.get('store_def') or {}).get('schema', '')
        # _go_expr resolves dotted keys (e.g. `request.coupon_code` → the
        # PascalCased typed-struct access or indexer-access for JsonNode
        # roots). Plain identifiers pass through unchanged.
        key_expr = _go_expr(inst['key'], ctx)
        if is_pg:
            lines.append(f"{pad}{assign}, err := {store}.Find(ctx, {key_expr})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{assign} := {store}.Find({key_expr})")
        # Find returns *Schema. Track the pointer-ness separately so downstream
        # STORE_UPDATE_FIELDS assigns without deref (pointer→pointer) and
        # _field_type_of still finds the underlying schema via lstrip('*').
        if store_schema:
            ctx.setdefault('locals', {})[assign] = '*' + store_schema

    elif op == 'STORE_FIND_BY':
        lines.extend(_compile_store_find_by_go(inst, indent, ctx))

    elif op == 'STORE_FILTER':
        lines.extend(_compile_store_filter_go(inst, indent, ctx))

    elif op == 'STORE_INSERT':
        assign = inst['assign']
        data = inst['data']
        locals_dict = ctx.setdefault('locals', {})
        data_type = locals_dict.get(data)
        store_schema = (ctx.get('store_def') or {}).get('schema', '')
        store_schema_go = ailang_type_to_go(store_schema) if store_schema else store_schema
        # In-memory store with a full entity (from CONSTRUCT) → persist via
        # Seed() so the PK stays caller-supplied. READ_BODY-result DTOs look
        # the same in locals but are actually `{Schema}CreateDTO` in Go —
        # the `_dto_vars` marker keeps them out of this branch and routes
        # them through normal `store.Insert(dto)`.
        dto_vars = ctx.get('_dto_vars') or set()
        if not is_pg and data_type == store_schema and data_type and data not in dto_vars:
            lines.append(f"{pad}{store}.Seed([]{store_schema_go}{{{data}}})")
            if assign != data:
                if assign in locals_dict:
                    lines.append(f"{pad}{assign} = {data}")
                else:
                    lines.append(f"{pad}{assign} := {data}")
                    locals_dict[assign] = data_type
        elif is_pg:
            decl = '=' if assign in locals_dict else ':='
            lines.append(f"{pad}{assign}, err {decl} {store}.Insert(ctx, {data})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
            locals_dict[assign] = store_schema
        else:
            decl = '=' if assign in locals_dict else ':='
            lines.append(f"{pad}{assign} {decl} {store}.Insert({data})")
            locals_dict[assign] = store_schema

    elif op == 'STORE_UPDATE':
        if is_pg:
            lines.append(f"{pad}{inst['assign']}, err := {store}.Update(ctx, {inst['key']}, {inst['data']})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{inst['assign']} := {store}.Update({inst['key']}, {inst['data']})")

    elif op == 'STORE_UPDATE_FIELDS':
        key = inst.get('key', 'id')
        assign = inst.get('assign')
        fields = inst.get('fields', {})
        store_schema = (ctx.get('store_def') or {}).get('schema', 'object')
        schema_def = (ctx.get('schemas') or {}).get(store_schema, {})
        schema_fields = schema_def.get('fields', {})
        # Build a map[string]any literal of the per-field new values. Field
        # values honor their schema type so callers can pass enum atoms as bare
        # identifiers (e.g. `cancelled`) — we quote them here.
        key_expr = _go_expr(key, ctx)
        parts = []
        for f, v in fields.items():
            fdef = schema_fields.get(f) or {}
            ftype = fdef.get('type')
            val = _go_expr(v, ctx, hint=ftype)
            if ftype in ('enum', 'string') and isinstance(v, str):
                known = (ctx.get('locals') or {})
                if v not in known and not v.startswith('"') and '.' not in v:
                    val = f'"{v}"'
            parts.append(f'"{f}": {val}')
        map_lit = f'map[string]any{{{", ".join(parts)}}}'
        locals_dict = ctx.setdefault('locals', {})
        if assign:
            # The store returns *{Schema}. Three cases for `assign`:
            #   1) Prior value-type (from CONSTRUCT): deref and guard nil.
            #   2) Prior pointer-type (from STORE_FIND / prior UpdateFields):
            #      pointer → pointer, straight assign.
            #   3) Not yet declared: fresh := with pointer type registered.
            prior_type = locals_dict.get(assign)
            if prior_type == store_schema:
                lines.append(f"{pad}if _updated := {store}.UpdateFields({key_expr}, {map_lit}); _updated != nil {{ {assign} = *_updated }}")
            elif prior_type == '*' + store_schema:
                lines.append(f"{pad}{assign} = {store}.UpdateFields({key_expr}, {map_lit})")
            elif assign in locals_dict:
                lines.append(f"{pad}{assign} = {store}.UpdateFields({key_expr}, {map_lit})")
            else:
                lines.append(f"{pad}{assign} := {store}.UpdateFields({key_expr}, {map_lit})")
                locals_dict[assign] = '*' + store_schema
        else:
            lines.append(f"{pad}{store}.UpdateFields({key_expr}, {map_lit})")

    elif op == 'STORE_DELETE':
        if is_pg:
            lines.append(f"{pad}if _, err := {store}.Delete(ctx, {inst['key']}); err != nil {{")
            lines.append(f"{pad}\trespondError(w, ErrInternal(err.Error())); return")
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{store}.Delete({inst['key']})")

    elif op == 'SQL_QUERY':
        lines.extend(_compile_sql_query_go(inst, indent, ctx))

    elif op == 'SQL_STREAM':
        lines.extend(_compile_sql_stream_go(inst, indent, ctx))

    elif op == 'SQL_EXEC':
        lines.extend(_compile_sql_exec_go(inst, indent, ctx))

    elif op == 'STREAM_RESPOND':
        lines.extend(_compile_stream_respond_go(inst, indent, ctx))

    elif op == 'RESPOND':
        status = inst.get('status', 200)
        body = inst.get('body')
        if status == 204 or body is None:
            lines.append(f"{pad}respondJSON(w, 204, nil)")
        else:
            lines.append(f"{pad}respondJSON(w, {status}, {body})")

    elif op == 'ERR_NOT_FOUND':
        id_expr = _go_value(inst["id"])
        lines.append(f'{pad}respondError(w, ErrNotFound("{inst["entity"]}", fmt.Sprint({id_expr})))')
        lines.append(f"{pad}return")

    elif op == 'ERR_INVALID_BODY':
        lines.append(f'{pad}respondError(w, ErrInvalidBody("{inst["schema"]}", "invalid"))')
        lines.append(f"{pad}return")

    elif op == 'ERR_UNAUTHORIZED':
        lines.append(f"{pad}respondError(w, ErrUnauthorized())")
        lines.append(f"{pad}return")

    elif op == 'ERR_BUSINESS_RULE':
        lines.append(f'{pad}respondError(w, ErrBusinessRule("{inst["rule"]}", "{inst["detail"]}"))')
        lines.append(f"{pad}return")

    elif op == 'ERR_CUSTOM':
        err_type = inst['type']
        params = inst.get('params', {}) or {}
        custom_errors = ctx.get('custom_errors', {}) or {}
        entry = custom_errors.get(err_type)
        if entry:
            fname = 'Err' + err_name_to_factory(err_type)
            declared = entry.get('params', []) or []
            # _go_expr so JsonNode dotted paths (e.g. product_result.stock)
            # resolve to `product_result["stock"]` instead of `.Stock`.
            args = [_go_expr(params.get(p, ""), ctx) for p in declared]
            lines.append(f'{pad}respondError(w, {fname}({", ".join(args)}))')
        else:
            tag = err_type_tag(err_type)
            lines.append(f'{pad}respondError(w, ErrBusinessRule("{tag}", ""))')
        lines.append(f"{pad}return")

    elif op == 'SAGA_COMPENSATE':
        saga = inst.get('saga', '')
        fn = 'compensate' + pascal_case(saga)
        ctx_expr = 'ctx' if ctx.get('in_event_handler') else 'r.Context()'
        lines.append(f"{pad}{fn}({ctx_expr})")

    elif op == 'PARALLEL':
        lines.extend(_compile_parallel_go(inst, indent, ctx))

    elif op == 'AWAIT_ALL':
        lines.extend(_compile_await_all_go(inst, indent, ctx))

    elif op == 'CALL_SERVICE':
        service = inst['service']
        method = inst.get('method', 'GET').upper()
        fmt, args = path_to_go_sprintf(inst.get('path', ''), ctx)
        assign = inst['assign']
        if args:
            path_expr = f'fmt.Sprintf("{fmt}", {", ".join(args)})'
        else:
            path_expr = f'"{fmt}"'
        body = inst.get('body')
        if body:
            # Use _go_expr(..., ctx) so dotted paths on JsonNode/map roots
            # (e.g. current_product.name after a prior CALL_SERVICE) resolve
            # to `current_product["name"]` instead of `current_product.Name`.
            pairs = ", ".join(f'"{k}": {_go_expr(v, ctx)}' for k, v in body.items())
            body_expr = f'map[string]any{{{pairs}}}'
        else:
            body_expr = 'nil'
        idempotent = 'true' if inst.get('idempotent', True) else 'false'
        if ctx.get('in_event_handler'):
            ctx_expr = 'ctx'
            urls_expr = f'_serviceURLs["{service}"]'
            # Event handlers don't have an http.ResponseWriter to respond with,
            # so we discard the call-error and leave downstream nil-check to fire.
            lines.append(
                f'{pad}{assign}, _ := callService({ctx_expr}, "{service}", {urls_expr}, "{method}", {path_expr}, {body_expr}, {idempotent})'
            )
        elif ctx.get('_in_parallel_fiber'):
            # Inside a PARALLEL fiber we can't touch the outer ResponseWriter
            # (racy) nor bail the handler with `return` (only exits the goroutine).
            # Graceful degradation patterns in these handlers already branch
            # on nil — so discard the error and let the existing nil-check
            # handle it.
            ctx_expr = 'r.Context()'
            urls_expr = f'rt.serviceURLs["{service}"]'
            lines.append(
                f'{pad}{assign}, _ := callService({ctx_expr}, "{service}", {urls_expr}, "{method}", {path_expr}, {body_expr}, {idempotent})'
            )
        else:
            ctx_expr = 'r.Context()'
            urls_expr = f'rt.serviceURLs["{service}"]'
            lines.append(
                f'{pad}{assign}, _callErr := callService({ctx_expr}, "{service}", {urls_expr}, "{method}", {path_expr}, {body_expr}, {idempotent})'
            )
            # Short-circuit when callService returned a typed transport error
            # (TIMEOUT/UNAVAILABLE). Generic failures come back as (nil, nil)
            # and are handled by the downstream `if assign == nil` check.
            lines.append(f'{pad}if _callErr != nil {{ respondError(w, _callErr); return }}')
        # callService returns `any` — objects decode as map[string]any, arrays as
        # []any. Register as 'any_json' so downstream knows to assert per context.
        ctx.setdefault('locals', {})[assign] = 'any_json'

    elif op == 'EMIT_EVENT':
        data_expr = _go_value(inst.get('data')) if inst.get('data') not in (None, '') else 'nil'
        if ctx.get('in_event_handler'):
            lines.append(f'{pad}_bus.Publish(ctx, "{inst["event"]}", {data_expr})')
        else:
            lines.append(f'{pad}rt.bus.Publish(r.Context(), "{inst["event"]}", {data_expr})')

    elif op == 'LOG':
        lines.append(f'{pad}log.Printf("[{inst.get("level", "INFO")}] {inst["message"]}")')

    # ── Phase 1: Variables and expressions ──
    elif op == 'DEFINE_VAR':
        lines.extend(_compile_define_var_go(inst, pad, ctx))

    elif op == 'SET_VAR':
        name = inst['name']
        hint = (ctx.get('locals') or {}).get(name)
        val_raw = inst.get('value')
        val = _go_expr(val_raw, ctx, hint=hint)
        # String/enum vars: quote bare atoms that aren't known locals (mirrors
        # the C# SET_VAR fix). Without this `overall_status = critical` emits
        # `critical` as an identifier → undefined.
        if hint in ('string', 'enum') and isinstance(val_raw, str):
            known = (ctx.get('locals') or {})
            if val_raw not in known and not val_raw.startswith('"') and '.' not in val_raw:
                try: float(val_raw)
                except ValueError: val = f'"{val_raw}"'
        lines.append(f"{pad}{name} = {val}")

    elif op == 'COMPUTE':
        operator = inst.get('operator', '+')
        assign = inst['assign']
        arith_ops = {'+', '-', '*', '/', '%'}
        cmp_ops = {'==', '!=', '<', '<=', '>', '>='}
        hint_left = hint_right = None
        def _is_json_path(v):
            """True when v is a dotted path whose root was set by CALL_SERVICE."""
            if not (isinstance(v, str) and '.' in v):
                return False
            root = v.split('.')[0]
            return (ctx.get('locals') or {}).get(root) in ('any_json', 'map', 'JsonNode')
        if operator in arith_ops:
            # Only hint as decimal when at least one side is a JsonNode path —
            # avoids wrapping pure-int arithmetic in float64().
            left_raw, right_raw = inst.get('left'), inst.get('right')
            if _is_json_path(left_raw) or _is_json_path(right_raw):
                hint_left = hint_right = 'decimal'
        elif operator in cmp_ops:
            hint_left = _scalar_type_hint(inst.get('right'))
            hint_right = _scalar_type_hint(inst.get('left'))
        left = _go_expr(inst.get('left'), ctx, hint=hint_left)
        right = _go_expr(inst.get('right'), ctx, hint=hint_right)
        locals_dict = ctx.setdefault('locals', {})
        if assign in locals_dict:
            lines.append(f"{pad}{assign} = {left} {operator} {right}")
        else:
            lines.append(f"{pad}{assign} := {left} {operator} {right}")
            locals_dict[assign] = 'inferred'

    elif op == 'COMPUTE_FN':
        lines.extend(_compile_compute_fn_go(inst, pad, ctx))

    elif op == 'TERNARY':
        # Go has no ternary — use a short inline IIFE with a small helper
        cond = _go_value(inst.get('condition'))
        then_v = _go_value(inst.get('then'))
        else_v = _go_value(inst.get('else'))
        assign = inst['assign']
        lines.append(f"{pad}var {assign} any; if {cond} {{ {assign} = {then_v} }} else {{ {assign} = {else_v} }}")

    elif op == 'IS_OK':
        lines.append(f"{pad}{inst['assign']} := {inst['value']} != nil")

    elif op == 'APPEND':
        target = inst['target']
        if 'value' in inst:
            lines.append(f"{pad}{target} = append({target}, {_go_expr(inst['value'], ctx)})")
        else:
            locals_dict = ctx.get('locals', {}) or {}
            ttype = locals_dict.get(target, 'List<object>')
            elem = extract_list_elem(ailang_type_to_go(ttype))
            fields = inst.get('object_fields', {})
            # Look up the elem schema to get per-field types, so `_go_expr`
            # can cast JsonNode paths correctly (product_result.name → string).
            elem_schema = (ctx.get('schemas') or {}).get(elem, {})
            elem_fields = elem_schema.get('fields', {}) if elem_schema else {}
            def _append_val(k, v):
                fdef = elem_fields.get(k) or {}
                ftype = fdef.get('type')
                # AILang convention in APPEND: dotted values are variable refs,
                # bare identifiers in a string/enum slot are atoms (string
                # literals) — even when the atom shadows a local variable name.
                # Pricing's `(type coupon)` has `coupon` as both the local var
                # AND the desired literal; APPEND field position disambiguates.
                if ftype in ('enum', 'string') and isinstance(v, str) \
                        and not v.startswith('"') and '.' not in v:
                    try:
                        float(v)
                        return _go_expr(v, ctx, hint=ftype)
                    except ValueError:
                        return f'"{v}"'
                return _go_expr(v, ctx, hint=ftype)
            init = ", ".join(f"{pascal_case(k)}: {_append_val(k, v)}" for k, v in fields.items())
            lines.append(f"{pad}{target} = append({target}, {elem}{{{init}}})")

    elif op == 'ITERATE':
        collection = inst['collection']
        item = inst['item']
        # Phase 7b: JsonNode-like collections (from CALL_SERVICE) are decoded as
        # map[string]any. To iterate as a list, cast the raw slice first. The
        # emitted `_<coll>_arr` is a temporary []any slice; `item` inside the
        # body becomes one element, registered as `map` so Phase 3b kicks in.
        locals_map = (ctx.get('locals') or {}) if ctx else {}
        coll_root = collection.split('.')[0] if isinstance(collection, str) else collection
        coll_type = locals_map.get(collection) or locals_map.get(coll_root)
        # For dotted paths on JsonNode-like roots (e.g. `data.items` inside an
        # EVENT_HANDLER), resolve via _go_resolve_path so the expression becomes
        # `data.(map[string]any)["items"]`. Sanitize for the temp var name so we
        # don't emit identifiers with dots.
        if isinstance(collection, str) and '.' in collection and coll_type in ('map', 'any_json'):
            coll_expr = _go_resolve_path(collection, ctx)
            arr_var = f"_{collection.replace('.', '_')}_arr"
            lines.append(f'{pad}{arr_var}, _ := ({coll_expr}).([]any)')
            lines.append(f"{pad}for _, _elem := range {arr_var} {{")
            lines.append(f'{pad}\t{item}, _ := _elem.(map[string]any)')
            # `item` is ALREADY typed as map[string]any via the assertion above,
            # so register as 'map' (no further initial assert needed). 'any_json'
            # would re-emit `.(map[string]any)` on every access — invalid on a
            # concrete type.
            item_type = 'map'
        elif coll_type in ('map', 'any_json'):
            arr_var = f"_{collection}_arr"
            lines.append(f'{pad}{arr_var}, _ := {collection}.([]any)')
            lines.append(f"{pad}for _, _elem := range {arr_var} {{")
            lines.append(f'{pad}\t{item}, _ := _elem.(map[string]any)')
            item_type = 'map'
        else:
            # Typed-struct collection: resolve dotted paths to PascalCase
            # (e.g. body.items → body.Items). `_go_resolve_path` does this
            # when the root is NOT any_json/map (falls through to typed access).
            coll_expr = _go_resolve_path(collection, ctx) if isinstance(collection, str) else collection
            lines.append(f"{pad}for _, {item} := range {coll_expr} {{")
            item_type = 'object'
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        inner_ctx['locals'][item] = item_type
        for sub in inst.get('body', []):
            lines.extend(compile_instruction_go(sub, indent + 1, inner_ctx))
        lines.append(f"{pad}}}")

    elif op == 'CONSTRUCT':
        schema = inst['schema']
        fields = inst.get('fields', {})
        assign = inst['assign']
        if schema == 'object':
            parts = []
            for k, v in fields.items():
                val = _go_construct_object_value(v, ctx)
                parts.append(f'"{k}": {val}')
            body = ", ".join(parts)
            locals_dict = ctx.setdefault('locals', {})
            if assign in locals_dict:
                lines.append(f"{pad}{assign} = map[string]any{{{body}}}")
            else:
                lines.append(f"{pad}{assign} := map[string]any{{{body}}}")
            # Register as 'any_json' so later dotted access (e.g.
            # products_section.status) goes through `.(map[string]any)["status"]`
            # instead of trying a typed struct field. The declared Go type may
            # be `any` (DEFINE_VAR) or `map[string]any` (fresh :=) — both work.
            locals_dict[assign] = 'any_json'
        else:
            schema_def = (ctx.get('schemas') or {}).get(schema, {})
            schema_fields = schema_def.get('fields', {})
            parts = []
            go_autogen = {'uuid': 'uuid.New().String()', 'timestamp': 'time.Now().UTC()',
                          'int': '0', 'bool': 'false', 'string': '""'}
            for sf_name, sf_def in schema_fields.items():
                if sf_def.get('auto_gen') and sf_name not in fields:
                    parts.append(f"{pascal_case(sf_name)}: {go_autogen.get(sf_def['type'], 'nil')}")
            for k, v in fields.items():
                fdef = schema_fields.get(k) or {}
                field_type = fdef.get('type')
                val = _go_expr(v, ctx, hint=field_type)
                if field_type in ('enum', 'string') and isinstance(v, str):
                    known = (ctx.get('locals') or {})
                    if v not in known and not v.startswith('"') and '.' not in v:
                        val = f'"{v}"'
                parts.append(f"{pascal_case(k)}: {val}")
            body = ", ".join(parts)
            lines.append(f"{pad}{assign} := {schema}{{{body}}}")
            ctx.setdefault('locals', {})[assign] = schema

    elif op == 'GET_CURRENT':
        from_ = inst.get('from', 'auth')
        field = inst.get('field', 'user')
        assign = inst['assign']
        locals_dict = ctx.setdefault('locals', {})
        if from_ == 'auth' and field == 'user':
            lines.append(f'{pad}{assign} := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")')
            locals_dict[assign] = 'string'
        elif from_ == 'clock' and field == 'now':
            lines.append(f"{pad}{assign} := _clock.Now()")
            locals_dict[assign] = 'timestamp'
        elif from_ == 'clock' and field == 'today':
            lines.append(f"{pad}{assign} := _clock.Today()")
            locals_dict[assign] = 'timestamp'
        elif from_ == 'clock' and field == 'day_of_week':
            lines.append(f"{pad}{assign} := _clock.DayOfWeek()")
            # Track the Weekday pseudo-type so BRANCH on bare `tuesday` etc.
            # resolves to `time.Tuesday` via _maybe_quote below.
            locals_dict[assign] = 'day_of_week'
        else:
            lines.append(f'{pad}var {assign} any  // GET_CURRENT {from_}.{field} (unsupported)')
        lines.append(f"{pad}_ = {assign}")

    elif op == 'MATCH':
        lines.extend(compile_match_go(inst, indent, ctx))

    elif op == 'BRANCH':
        lines.extend(compile_branch_go(inst, indent, ctx))

    return lines


def _go_json_cast(expr: str, hint: str) -> str:
    """Wrap a map-indexer expression with the Go type assertion that matches `hint`.

    JSON decoding via encoding/json produces float64 for all numbers, so int/decimal
    both start as float64; we just downcast ints with int(). Missing or wrong-type
    fields produce the zero value of the asserted type — loose, but consistent with
    how untyped inter-service payloads already behave in idiomatic Go."""
    if not hint:
        return expr
    if hint in ('string', 'uuid'):
        return f'({expr}).(string)'
    if hint == 'bool':
        return f'({expr}).(bool)'
    if hint == 'int':
        return f'int(({expr}).(float64))'
    if hint == 'decimal':
        return f'({expr}).(float64)'
    if hint == 'timestamp':
        return f'({expr}).(string)'  # ISO-8601 string; caller reparses if needed
    return expr


def _go_resolve_path(expr: str, ctx: dict, hint: str = None) -> str:
    if not isinstance(expr, str) or '.' not in expr:
        return expr
    parts = expr.split('.')
    if not parts or not (parts[0] and (parts[0][0].isalpha() or parts[0][0] == '_')):
        return expr
    root_type = (ctx.get('locals') or {}).get(parts[0]) if ctx else None
    if root_type in ('map', 'any_json'):
        # Root may be `any` (callService) or already asserted to map[string]any.
        # First access needs `.(map[string]any)` assertion when root is raw any.
        needs_initial_assert = (root_type == 'any_json')
        inner = parts[0]
        for i, seg in enumerate(parts[1:], start=1):
            if i == 1 and needs_initial_assert:
                inner = f'{inner}.(map[string]any)["{seg}"]'
            elif i < len(parts) - 1:
                inner = f'{inner}["{seg}"].(map[string]any)'
            else:
                inner = f'{inner}["{seg}"]'
        if hint and hint not in ('list', 'object'):
            return _go_json_cast(inner, hint)
        return inner
    # Typed struct field access. When the hint is numeric and the path is a typed
    # struct field, widen to float64 so mixed JsonNode*struct arithmetic compiles
    # (Go doesn't have implicit numeric widening).
    typed_expr = parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])
    if hint == 'decimal':
        return f'float64({typed_expr})'
    return typed_expr


def _go_expr(v, ctx: dict = None, hint: str = None) -> str:
    if ctx and isinstance(v, str) and '.' in v:
        s = v
        if s and (s[0].isalpha() or s[0] == '_') and all(c.isalnum() or c in '_.' for c in s):
            return _go_resolve_path(s, ctx, hint)
    base = _go_value(v, ctx)
    if ctx and isinstance(v, str):
        known_type = (ctx.get('locals') or {}).get(v)
        # Widen plain identifiers whose known type is int when used in a decimal
        # context (Go has no implicit numeric conversions).
        if hint == 'decimal' and known_type == 'int':
            return f'float64({base})'
        # When passing an any-typed local into a struct field whose declared
        # type is object/map, emit the type assertion so Go accepts the value.
        if hint == 'object' and known_type == 'any_json':
            return f'{base}.(map[string]any)'
    return base


def _go_construct_object_value(v, ctx: dict) -> str:
    """Render a CONSTRUCT-object field value as a Go expression.

    Mirrors `_cs_construct_object_value` — scalars/identifiers pass through
    `_go_expr` with string-atom quoting; a list-of-pairs whose first pair is
    `['op', 'TERNARY']` or `['op', 'IS_OK']` is expanded inline so nested
    ternary/is_ok checks don't fall through to the TODO bailout.
    """
    known = (ctx.get('locals') or {})
    if isinstance(v, list) and v and all(isinstance(p, list) and len(p) >= 2 for p in v):
        op_dict = {}
        for pair in v:
            key = str(pair[0])
            op_dict[key] = pair[1] if len(pair) == 2 else pair[1:]
        if op_dict.get('op') == 'TERNARY':
            cond_expr = _go_construct_object_value(op_dict.get('condition'), ctx)
            then_expr = _go_construct_object_value(op_dict.get('true'), ctx)
            else_expr = _go_construct_object_value(op_dict.get('false'), ctx)
            # Go has no ternary — map to an immediately-invoked func literal
            # returning `any` so the enclosing map[string]any literal accepts it.
            return f'func() any {{ if {cond_expr} {{ return {then_expr} }}; return {else_expr} }}()'
        if op_dict.get('op') == 'IS_OK':
            left = op_dict.get('left') or op_dict.get('value')
            left_expr = _go_construct_object_value(left, ctx)
            return f'({left_expr} != nil)'
        # Plain nested object → map[string]any literal.
        parts = []
        for pair in v:
            k = str(pair[0])
            inner_val = pair[1] if len(pair) == 2 else pair[1:]
            parts.append(f'"{k}": {_go_construct_object_value(inner_val, ctx)}')
        return 'map[string]any{' + ', '.join(parts) + '}'
    val = _go_expr(v, ctx)
    if isinstance(v, str) and v not in known and not v.startswith('"') and '.' not in v:
        try:
            float(v)
        except ValueError:
            if v.lower() not in ('true', 'false', 'null', 'nil'):
                val = f'"{v}"'
    return val


def _go_value(v, ctx: dict = None) -> str:
    from sexpr_parser import StringLiteral
    if v is None:
        return 'nil'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, StringLiteral):
        escaped = str(v).replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        # Pair-list → nested map literal. Every sub-element must be a 2-tuple
        # `[key, value]`; otherwise fall through to the opaque TODO sentinel.
        if all(isinstance(item, list) and len(item) == 2 for item in v):
            parts = []
            for k, inner in v:
                key = str(k)
                # Recursively format the value; identifiers resolve via ctx
                # when available, else pass through _go_value.
                if ctx is not None and isinstance(inner, str):
                    val_str = _go_expr(inner, ctx)
                else:
                    val_str = _go_value(inner, ctx)
                parts.append(f'"{key}": {val_str}')
            return f'map[string]any{{{", ".join(parts)}}}'
        return f'/* TODO nested: {v} */ nil'
    s = str(v)
    if s.startswith('"'):
        return s
    try:
        float(s)
        return s
    except ValueError:
        pass
    if s and (s[0].isalpha() or s[0] == '_') and all(c.isalnum() or c in '_.' for c in s):
        if '.' in s:
            parts = s.split('.')
            return parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])
        return s
    escaped = s.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _compile_define_var_go(inst: dict, pad: str, ctx: dict) -> list[str]:
    from sexpr_parser import StringLiteral
    name = inst['name']
    vtype = inst.get('type', 'object')
    value = inst.get('value')
    go_type = ailang_type_to_go(vtype)
    ctx.setdefault('locals', {})[name] = vtype

    if isinstance(value, dict) and value.get('__empty_list__'):
        return [f"{pad}{name} := make({go_type}, 0)"]
    if value is None:
        return [f"{pad}var {name} {go_type}"]
    if isinstance(value, bool):
        return [f"{pad}{name} := {'true' if value else 'false'}"]
    if isinstance(value, StringLiteral):
        return [f"{pad}var {name} {go_type} = {_go_value(value)}"]
    if isinstance(value, (int, float)):
        return [f"{pad}var {name} {go_type} = {value}"]
    s = str(value)
    if s.startswith('"'):
        return [f"{pad}var {name} {go_type} = {s}"]
    if vtype == 'string':
        # Bare identifier default for a string-typed var is a string atom.
        known = ctx.get('locals') or {}
        if isinstance(value, str) and value not in known and not value.startswith('"') and '.' not in value:
            try:
                float(value)
            except ValueError:
                return [f'{pad}var {name} {go_type} = "{value}"']
        return [f"{pad}var {name} {go_type} = {_go_value(value)}"]
    if vtype == 'object' and s.startswith('{'):
        return [f"{pad}var {name} {go_type}  // TODO: object initializer {s}"]
    return [f"{pad}var {name} {go_type} = {s}"]


def _compile_compute_fn_go(inst: dict, pad: str, ctx: dict) -> list[str]:
    fn = inst['function']
    raw_args = inst.get('args', [])
    args = [_go_value(a) for a in raw_args]
    assign = inst['assign']
    locals_dict = ctx.setdefault('locals', {})
    was_declared = assign in locals_dict
    decl = f"{assign} =" if was_declared else f"{assign} :="
    # Register the new local so downstream refs (e.g. CONSTRUCT "recent":
    # recent_orders) resolve as identifiers instead of being quoted as atoms.
    if not was_declared:
        locals_dict[assign] = 'object'
    fn_map = {
        'round': 'math.Round', 'floor': 'math.Floor', 'ceil': 'math.Ceil',
        'min': 'min', 'max': 'max', 'abs': 'math.Abs',
    }
    if fn == 'length':
        src = args[0] if args else 'nil'
        src_raw = str(raw_args[0]) if raw_args else ''
        src_type = locals_dict.get(src_raw)
        if src_type in ('any_json', 'map'):
            return [f"{pad}{decl} len({src}.([]any))"]
        return [f"{pad}{decl} len({src})"]
    if fn == 'take_last':
        src = args[0] if args else 'nil'
        n = args[1] if len(args) > 1 else '5'
        src_raw = str(raw_args[0]) if raw_args else ''
        src_type = locals_dict.get(src_raw)
        if src_type in ('any_json', 'map'):
            return [f"{pad}_arr := {src}.([]any); _start := len(_arr) - {n}; if _start < 0 {{ _start = 0 }}; {decl} _arr[_start:]"]
        return [f"{pad}_start := len({src}) - {n}; if _start < 0 {{ _start = 0 }}; {decl} {src}[_start:]"]
    # round(x, n) → math.Round(x*10^n) / 10^n. Go's math.Round is arity-1.
    if fn == 'round' and len(args) == 2:
        factor = f"math.Pow(10, {args[1]})"
        return [f"{pad}{decl} math.Round({args[0]}*{factor}) / {factor}"]
    if fn in fn_map:
        return [f"{pad}{decl} {fn_map[fn]}({', '.join(args)})"]
    return [f"{pad}var {assign} any  // COMPUTE_FN: unsupported function {fn}"]


def _pg_select_columns_sql_go(store_def: dict) -> str:
    return ", ".join(m['column'] for _, m in store_def.get('mapping', {}).items())


def _compile_store_find_by_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "\t" * indent
    store_def = ctx['store_def']
    store = ctx['store_accessor']
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    where_sql = where_to_sql(inst.get('where'))
    cols_sql = _pg_select_columns_sql_go(store_def)
    sql = f'SELECT {cols_sql}\nFROM {table}\nWHERE {where_sql}\nLIMIT 1'
    sql_lit = emit_sql_literal_go(sql)
    assign = inst['assign']
    lines = [
        f"{pad}var {assign} *{schema}",
        f"{pad}{{",
        f'{pad}\t_args := pgx.NamedArgs{{}}',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}\t_args["{p["name"]}"] = {p["var"]}')
    lines.append(f'{pad}\t_row := {store}.Pool().QueryRow(ctx, {sql_lit}, _args)')
    lines.append(f"{pad}\t_p, _err := scan{schema}(_row)")
    lines.append(f"{pad}\tif _err != nil && !errors.Is(_err, pgx.ErrNoRows) {{")
    lines.append(f"{pad}\t\trespondError(w, ErrInternal(_err.Error())); return")
    lines.append(f"{pad}\t}}")
    lines.append(f"{pad}\tif _err == nil {{ {assign} = &_p }}")
    lines.append(f"{pad}}}")
    return lines


def _compile_store_filter_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "\t" * indent
    store_def = ctx['store_def']
    store = ctx['store_accessor']
    schema = store_def['schema']
    table = store_def.get('table') or schema.lower() + 's'
    where_sql = where_to_sql(inst.get('where'))
    cols_sql = _pg_select_columns_sql_go(store_def)
    assign = inst['assign']
    paginate = inst.get('paginate') or {}
    page_var = paginate.get('page') or 1
    limit_var = paginate.get('limit') or 20
    max_limit = paginate.get('max_limit') or 100

    count_sql = f'SELECT COUNT(*)\nFROM {table}\nWHERE {where_sql}'
    data_sql = (
        f'SELECT {cols_sql}\n'
        f'FROM {table}\n'
        f'WHERE {where_sql}\n'
        f'ORDER BY id\n'
        f'LIMIT @_limit OFFSET @_offset'
    )
    count_lit = emit_sql_literal_go(count_sql)
    data_lit = emit_sql_literal_go(data_sql)

    lines = [
        f"{pad}var {assign} Paginated[{schema}]",
        f"{pad}{{",
        f"{pad}\t_limit := {limit_var}",
        f"{pad}\tif _limit < 1 {{ _limit = 20 }}",
        f"{pad}\tif _limit > {max_limit} {{ _limit = {max_limit} }}",
        f"{pad}\t_page := {page_var}",
        f"{pad}\tif _page < 1 {{ _page = 1 }}",
        f"{pad}\t_offset := (_page - 1) * _limit",
        f"{pad}\t_args := pgx.NamedArgs{{}}",
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}\t_args["{p["name"]}"] = {p["var"]}')
    lines.append(f"{pad}\tvar _total int64")
    lines.append(f'{pad}\tif _err := {store}.Pool().QueryRow(ctx, {count_lit}, _args).Scan(&_total); _err != nil {{')
    lines.append(f"{pad}\t\trespondError(w, ErrInternal(_err.Error())); return")
    lines.append(f"{pad}\t}}")
    lines.append(f'{pad}\t_args["_limit"] = _limit')
    lines.append(f'{pad}\t_args["_offset"] = _offset')
    lines.append(f'{pad}\t_rows, _err := {store}.Pool().Query(ctx, {data_lit}, _args)')
    lines.append(f"{pad}\tif _err != nil {{ respondError(w, ErrInternal(_err.Error())); return }}")
    lines.append(f"{pad}\tdefer _rows.Close()")
    lines.append(f"{pad}\t_items := make([]{schema}, 0)")
    lines.append(f"{pad}\tfor _rows.Next() {{")
    lines.append(f"{pad}\t\t_p, _err2 := scan{schema}(_rows)")
    lines.append(f"{pad}\t\tif _err2 != nil {{ respondError(w, ErrInternal(_err2.Error())); return }}")
    lines.append(f"{pad}\t\t_items = append(_items, _p)")
    lines.append(f"{pad}\t}}")
    lines.append(f"{pad}\t_totalPages := 0")
    lines.append(f"{pad}\tif _limit > 0 {{ _totalPages = int((_total + int64(_limit) - 1) / int64(_limit)) }}")
    lines.append(f"{pad}\t{assign} = Paginated[{schema}]{{Data: _items, Page: _page, Limit: _limit, Total: _total, TotalPages: _totalPages}}")
    lines.append(f"{pad}}}")
    return lines


def _compile_parallel_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Emit a goroutine per fiber, each writing its last-assign into a
    per-fiber shared variable. An anonymous-struct `parallel_results` is
    materialized by the AWAIT_ALL helper below once the WaitGroup drains
    (or the timeout fires)."""
    pad = "\t" * indent
    assign = inst['assign']
    fibers = inst.get('fibers', []) or []

    lines = [f"{pad}// ── PARALLEL {assign}: {len(fibers)} fiber(s) ──"]
    fiber_result_vars = []  # [(fiber_name, shared_var, result_var), ...]

    # Precompute each fiber's inner result_var — same logic as C# side — so the
    # PARALLEL metadata records it for downstream MATCH rebinding.
    fiber_result_lookup = {}
    for fiber in fibers:
        fname = fiber['name']
        shared = f"_pres_{assign}_{fname}"
        rv = None
        for f_inst in fiber.get('instructions', []):
            if f_inst.get('assign'):
                rv = f_inst['assign']
        fiber_result_lookup[fname] = rv
        fiber_result_vars.append((fname, shared, rv))
        lines.append(f"{pad}var {shared} any")

    lines.append(f"{pad}var _wg_{assign} sync.WaitGroup")
    lines.append(f"{pad}_wg_{assign}.Add({len(fibers)})")

    for fiber in fibers:
        fname = fiber['name']
        shared = f"_pres_{assign}_{fname}"
        result_var = fiber_result_lookup.get(fname)
        lines.append(f"{pad}go func() {{")
        lines.append(f"{pad}\tdefer _wg_{assign}.Done()")
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        # Flag used by CALL_SERVICE emission so it doesn't try to touch the
        # outer ResponseWriter or bail the enclosing handler from inside a
        # goroutine — leaves the nil result for AWAIT_ALL to fall through.
        inner_ctx['_in_parallel_fiber'] = True
        for f_inst in fiber.get('instructions', []):
            lines.extend(compile_instruction_go(f_inst, indent + 1, inner_ctx))
        if result_var:
            lines.append(f"{pad}\t{shared} = {result_var}")
        lines.append(f"{pad}}}()")

    parallel_map = ctx.setdefault('_parallel_tasks', {})
    parallel_map[assign] = fiber_result_vars
    ctx.setdefault('locals', {})[assign] = 'ParallelResults'
    return lines


def _compile_await_all_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Join the WaitGroup from the matching PARALLEL with a timeout, then build
    an anonymous-struct value with PascalCased fields matching fiber names
    (so downstream `parallel_results.fetch_products` PascalCases to
    `parallel_results.FetchProducts`, resolvable as a struct field)."""
    pad = "\t" * indent
    source = inst['source']
    timeout_ms = inst.get('timeout_ms') or 0
    parallel_map = (ctx or {}).get('_parallel_tasks', {})
    fibers = parallel_map.get(source, [])
    if not fibers:
        return [f"{pad}// WARN: AWAIT_ALL {source} — no matching PARALLEL block"]

    lines = [f"{pad}// ── AWAIT_ALL {source} (timeout {timeout_ms}ms) ──"]
    lines.append(f"{pad}_done_{source} := make(chan struct{{}})")
    lines.append(f"{pad}go func() {{ _wg_{source}.Wait(); close(_done_{source}) }}()")
    if timeout_ms and timeout_ms > 0:
        lines.append(f"{pad}select {{")
        lines.append(f"{pad}case <-_done_{source}:")
        lines.append(f"{pad}case <-time.After({timeout_ms} * time.Millisecond):")
        lines.append(f'{pad}\tslog.Warn("parallel timeout", "name", "{source}", "timeout_ms", {timeout_ms})')
        lines.append(f"{pad}}}")
    else:
        lines.append(f"{pad}<-_done_{source}")

    # Build anonymous struct — Go inline struct field separator is `;`, not `,`.
    struct_fields = "; ".join(f"{pascal_case(fn)} any" for fn, _, _ in fibers)
    field_inits = ", ".join(f"{pascal_case(fn)}: {sv}" for fn, sv, _ in fibers)
    lines.append(f"{pad}{source} := struct {{ {struct_fields} }}{{{field_inits}}}")
    lines.append(f"{pad}_ = {source}  // suppress unused warning when handler reads fields conditionally")
    return lines


def _compile_sql_query_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "\t" * indent
    store = ctx['store_accessor']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_go(sql)
    assign = inst['assign']
    target_type = (ctx.get('returns') or {}).get('type') or 'any'
    lines = [
        f"{pad}{assign} := make([]{target_type}, 0)",
        f"{pad}{{",
        f'{pad}\t_args := pgx.NamedArgs{{}}',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}\t_args["{p["name"]}"] = {p["var"]}')
    lines.append(f'{pad}\t_rows, _err := {store}.Pool().Query(ctx, {sql_lit}, _args)')
    lines.append(f"{pad}\tif _err != nil {{ respondError(w, ErrInternal(_err.Error())); return }}")
    lines.append(f"{pad}\tdefer _rows.Close()")
    lines.append(f"{pad}\tfor _rows.Next() {{")
    lines.append(f"{pad}\t\t_row, _err2 := readRow{target_type}(_rows)")
    lines.append(f"{pad}\t\tif _err2 != nil {{ respondError(w, ErrInternal(_err2.Error())); return }}")
    lines.append(f"{pad}\t\t{assign} = append({assign}, _row)")
    lines.append(f"{pad}\t}}")
    lines.append(f"{pad}}}")
    return lines


def _compile_sql_stream_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Emit Go 1.23+ iter.Seq2 iterator over rows as (map, error).

    iter.Seq2[map[string]any, error] is canonical for streams that can fail —
    STREAM_RESPOND consumes it with `for row, err := range {assign} { ... }`.
    """
    pad = "\t" * indent
    store = ctx['store_accessor']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_go(sql)
    assign = inst['assign']
    lines = [
        f"{pad}{assign} := func(yield func(map[string]any, error) bool) {{",
        f'{pad}\t_args := pgx.NamedArgs{{}}',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}\t_args["{p["name"]}"] = {p["var"]}')
    lines.append(f'{pad}\t_rows, _err := {store}.Pool().Query(ctx, {sql_lit}, _args)')
    lines.append(f"{pad}\tif _err != nil {{ yield(nil, _err); return }}")
    lines.append(f"{pad}\tdefer _rows.Close()")
    lines.append(f"{pad}\tfor _rows.Next() {{")
    lines.append(f"{pad}\t\t_vals, _e := _rows.Values()")
    lines.append(f"{pad}\t\tif _e != nil {{ yield(nil, _e); return }}")
    lines.append(f"{pad}\t\t_fds := _rows.FieldDescriptions()")
    lines.append(f"{pad}\t\t_row := make(map[string]any, len(_fds))")
    lines.append(f"{pad}\t\tfor _i, _fd := range _fds {{ _row[string(_fd.Name)] = _vals[_i] }}")
    lines.append(f"{pad}\t\tif !yield(_row, nil) {{ return }}")
    lines.append(f"{pad}\t}}")
    lines.append(f"{pad}\tif _e := _rows.Err(); _e != nil {{ yield(nil, _e) }}")
    lines.append(f"{pad}}}")
    return lines


def emit_sql_literal_go(sql: str) -> str:
    """Emit a Go string literal for a SQL query.

    Uses a raw-string backtick literal for multi-line SQL (readable, no escaping);
    falls back to a plain double-quoted string for single-line.
    """
    lines = [l.rstrip() for l in sql.splitlines() if l.strip()]
    if len(lines) <= 1:
        one = ' '.join(l.strip() for l in lines)
        escaped = one.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    # Multi-line → backtick raw string. Backticks themselves cannot appear inside,
    # so use plain double-quoted strings for any SQL that contains backticks.
    joined = '\n'.join(l.strip() for l in lines)
    if '`' in joined:
        escaped = joined.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        return f'"{escaped}"'
    return f'`{joined}`'


def _compile_sql_exec_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    pad = "\t" * indent
    store = ctx['store_accessor']
    sql = inst.get('sql', '')
    sql_lit = emit_sql_literal_go(sql)
    assign = inst.get('assign') or '_affected'
    lines = [
        f"{pad}var {assign} int64",
        f"{pad}{{",
        f'{pad}\t_args := pgx.NamedArgs{{}}',
    ]
    for p in inst.get('params', []) or []:
        lines.append(f'{pad}\t_args["{p["name"]}"] = {p["var"]}')
    lines.append(f'{pad}\t_tag, _err := {store}.Pool().Exec(ctx, {sql_lit}, _args)')
    lines.append(f"{pad}\tif _err != nil {{ respondError(w, ErrInternal(_err.Error())); return }}")
    lines.append(f"{pad}\t{assign} = _tag.RowsAffected()")
    lines.append(f"{pad}}}")
    return lines


def _compile_stream_respond_go(inst: dict, indent: int, ctx: dict) -> list[str]:
    """Consume a Go 1.23 iter.Seq2[map[string]any, error] and stream as NDJSON."""
    pad = "\t" * indent
    stream = inst.get('stream')
    status = inst.get('status', 200)
    content_type = inst.get('content_type', 'application/x-ndjson')
    return [
        f'{pad}w.Header().Set("Content-Type", "{content_type}")',
        f"{pad}w.WriteHeader({status})",
        f"{pad}_flusher, _ := w.(http.Flusher)",
        f"{pad}for _row, _err := range {stream} {{",
        f'{pad}\tif _err != nil {{ slog.Error("stream error", "err", _err); return }}',
        f"{pad}\t_data, _ := json.Marshal(_row)",
        f"{pad}\tw.Write(_data)",
        f'{pad}\tw.Write([]byte("\\n"))',
        f"{pad}\tif _flusher != nil {{ _flusher.Flush() }}",
        f"{pad}}}",
    ]


def compile_match_go(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    pad = "\t" * indent
    lines = []
    # Member access like `parallel_results.fetch_products` must PascalCase the
    # trailing segments to match Go struct field naming.
    raw_value = inst['value']
    value = _go_value(raw_value)
    cases = inst.get('cases', {})

    # Phase 7b: parallel-result MATCH rebinds the fiber's inner result_var.
    rebind_var = None
    rebind_stmt = None
    if isinstance(raw_value, str) and '.' in raw_value:
        source, _, fname = raw_value.partition('.')
        fibers = ((ctx or {}).get('_parallel_tasks') or {}).get(source) or []
        for fn, _sv, rv in fibers:
            if fn == fname and rv:
                rebind_var = rv
                rebind_stmt = f"{rv} := {source}.{pascal_case(fname)}"
                break

    if 'OK' in cases and 'NONE' in cases:
        # OK + NONE → pointer/nullable check (store.Find pattern).
        # If the NONE branch always returns (err response), keep the old flat
        # layout so OK continues at the outer indent. Otherwise wrap OK in an
        # `else` so it doesn't execute with the nil value.
        none_lines = []
        for sub in cases['NONE']:
            none_lines.extend(compile_instruction_go(sub, indent + 1, ctx))
        none_returns = any(l.strip().startswith('return') for l in none_lines)

        lines.append(f"{pad}if {value} == nil {{")
        lines.extend(none_lines)
        if none_returns:
            lines.append(f"{pad}}}")
            if rebind_stmt:
                lines.append(f"{pad}{rebind_stmt}")
                lines.append(f"{pad}_ = {rebind_var}")
                ctx.setdefault('locals', {})[rebind_var] = 'any_json'
            for sub in cases['OK']:
                lines.extend(compile_instruction_go(sub, indent, ctx))
        else:
            lines.append(f"{pad}}} else {{")
            if rebind_stmt:
                lines.append(f"{pad}\t{rebind_stmt}")
                lines.append(f"{pad}\t_ = {rebind_var}")
                ctx.setdefault('locals', {})[rebind_var] = 'any_json'
            for sub in cases['OK']:
                lines.extend(compile_instruction_go(sub, indent + 1, ctx))
            lines.append(f"{pad}}}")
    elif 'OK' in cases and 'ERR' in cases:
        # OK + ERR: two interpretations depending on the value's type.
        #   - If value is any_json / map / JsonNode (typical CALL_SERVICE result),
        #     ERR means the call failed → the value is nil. Emit a nil-check with
        #     both branches, same shape as OK+NONE.
        #   - PARALLEL fiber results (parallel_results.fetch_X) also land here:
        #     the struct field holds `any`, nil when the fiber's CALL_SERVICE
        #     returned nil. Treat as nullable.
        #   - If value is a typed struct (READ_BODY result), decode errors were
        #     already handled by READ_BODY, so ERR is dead code — emit OK inline.
        raw_root = raw_value.split('.')[0] if isinstance(raw_value, str) else None
        root_type = (ctx.get('locals') or {}).get(raw_root) if raw_root else None
        is_parallel_field = isinstance(raw_value, str) and '.' in raw_value and root_type == 'ParallelResults'
        is_nullable_value = root_type in ('any_json', 'map', 'JsonNode') or is_parallel_field
        if is_nullable_value:
            err_lines = []
            for sub in cases['ERR']:
                err_lines.extend(compile_instruction_go(sub, indent + 1, ctx))
            err_returns = any(l.strip().startswith('return') for l in err_lines)
            lines.append(f"{pad}if {value} == nil {{")
            lines.extend(err_lines)
            if err_returns:
                lines.append(f"{pad}}}")
                if rebind_stmt:
                    lines.append(f"{pad}{rebind_stmt}")
                    lines.append(f"{pad}_ = {rebind_var}")
                    ctx.setdefault('locals', {})[rebind_var] = 'any_json'
                for sub in cases['OK']:
                    lines.extend(compile_instruction_go(sub, indent, ctx))
            else:
                lines.append(f"{pad}}} else {{")
                if rebind_stmt:
                    lines.append(f"{pad}\t{rebind_stmt}")
                    lines.append(f"{pad}\t_ = {rebind_var}")
                    ctx.setdefault('locals', {})[rebind_var] = 'any_json'
                for sub in cases['OK']:
                    lines.extend(compile_instruction_go(sub, indent + 1, ctx))
                lines.append(f"{pad}}}")
        else:
            if rebind_stmt:
                lines.append(f"{pad}{rebind_stmt}")
                lines.append(f"{pad}_ = {rebind_var}")
                ctx.setdefault('locals', {})[rebind_var] = 'any_json'
            for sub in cases['OK']:
                lines.extend(compile_instruction_go(sub, indent, ctx))
    else:
        # Value-dispatch MATCH (integer / string arms — not RESULT pattern).
        known = (ctx.get('locals') or {})
        all_int = all(isinstance(k, int) or (isinstance(k, str) and k.isdigit()) for k in cases if str(k) != 'DEFAULT')
        value_hint = 'int' if all_int else 'string'
        value_expr = _go_expr(raw_value, ctx, hint=value_hint)
        first = True
        for arm_key, arm_body in cases.items():
            arm_str = str(arm_key)
            if arm_str == 'DEFAULT':
                lines.append(f"{pad}}} else {{")
            else:
                arm_val = _go_value(arm_key)
                if isinstance(arm_key, str) and not arm_key.startswith('"'):
                    try: int(arm_key)
                    except ValueError: arm_val = f'"{arm_key}"'
                if first:
                    lines.append(f"{pad}if {value_expr} == {arm_val} {{")
                    first = False
                else:
                    lines.append(f"{pad}}} else if {value_expr} == {arm_val} {{")
            for sub in arm_body:
                lines.extend(compile_instruction_go(sub, indent + 1, ctx))
        if cases:
            lines.append(f"{pad}}}")

    return lines


def compile_branch_go(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    pad = "\t" * indent
    lines = []
    condition = inst.get('condition', '')

    if isinstance(condition, dict):
        left_raw = condition.get('left', '')
        right_raw = condition.get('right', '')
        hint_left = _scalar_type_hint(right_raw)
        hint_right = _scalar_type_hint(left_raw)
        # When a side is a JsonNode-rooted path (any_json/map/JsonNode), it's
        # `any` in Go and needs a type assertion to compare/compute. Default to
        # 'decimal' for ordered ops so both sides end up as float64, matching
        # how encoding/json decodes numbers.
        locals_map = (ctx.get('locals') or {}) if ctx else {}
        def _is_json_path(v):
            if not (isinstance(v, str) and '.' in v):
                return False
            root = v.split('.')[0]
            return locals_map.get(root) in ('any_json', 'map', 'JsonNode')
        op_lookup = condition.get('op', '==')
        if op_lookup in ('<', '<=', '>', '>=', '+', '-', '*', '/'):
            # If one side is a JsonNode path, both sides end up as float64.
            # Hint the JsonNode side so its `.(float64)` assertion is emitted,
            # and hint the other side too so a typed int gets `float64(...)`
            # widening (Go has no implicit numeric conversion).
            if _is_json_path(left_raw) or _is_json_path(right_raw):
                if hint_left is None:
                    hint_left = 'decimal'
                if hint_right is None:
                    hint_right = 'decimal'
        # Typed-struct field or typed local on one side + bare identifier on
        # the other (not a local) → the identifier is a string/enum atom. Map
        # to a Go literal.
        def _field_type_of(path):
            if not isinstance(path, str):
                return None
            if '.' not in path:
                # Plain local identifier — look up its registered type.
                return locals_map.get(path)
            root = path.split('.')[0]
            root_type = locals_map.get(root)
            if not root_type or root_type in ('any_json', 'map', 'JsonNode'):
                return None
            # `*Schema` (from STORE_FIND) → Schema for the schemas lookup.
            schema_name = root_type.lstrip('*')
            schema = (ctx.get('schemas') or {}).get(schema_name, {})
            fields = schema.get('fields', {})
            leaf = path.split('.')[-1]
            fdef = fields.get(leaf) or {}
            return fdef.get('type')
        def _maybe_quote(side_raw, other_field_type, other_is_json):
            if not isinstance(side_raw, str):
                return None
            if side_raw.startswith('"') or '.' in side_raw:
                return None
            if side_raw in locals_map:
                return None
            if side_raw in ('true', 'false'):
                return None  # bool literal, not a string atom
            try:
                float(side_raw)
                return None
            except ValueError:
                pass
            if other_field_type == 'day_of_week':
                return f'time.{side_raw.capitalize()}'
            if side_raw == 'null' and (other_field_type in ('string', 'enum') or other_is_json):
                return '""'
            if other_field_type in ('string', 'enum'):
                return f'"{side_raw}"'
            # JsonNode accessor on the other side: assume the decoded value is
            # a string (equality check against a bare atom is the idiomatic
            # shape for status/type checks). Quote defensively.
            if other_is_json:
                return f'"{side_raw}"'
            return None
        l_ftype = _field_type_of(left_raw)
        r_ftype = _field_type_of(right_raw)
        right_quoted = _maybe_quote(right_raw, l_ftype, _is_json_path(left_raw))
        left_quoted = _maybe_quote(left_raw, r_ftype, _is_json_path(right_raw))
        left = left_quoted if left_quoted is not None else _go_expr(left_raw, ctx, hint=hint_left)
        op = condition.get('op', '==')
        right = right_quoted if right_quoted is not None else _go_expr(right_raw, ctx, hint=hint_right)
        if op == 'NOT_IN':
            cond_str = f"!_sliceContains({right}, {left})"
        elif op == 'IN':
            cond_str = f"_sliceContains({right}, {left})"
        elif op in ('<', '<=', '>', '>=') and (l_ftype == 'timestamp' or r_ftype == 'timestamp'):
            # time.Time doesn't support Go's ordering operators; use methods.
            method = {'>': 'After', '<': 'Before', '>=': 'After', '<=': 'Before'}[op]
            cond_str = f"{left}.{method}({right})"
            # For `>=` / `<=` emit `!Before(...)` / `!After(...)` to flip.
            if op == '>=':
                cond_str = f"!{left}.Before({right})"
            elif op == '<=':
                cond_str = f"!{left}.After({right})"
        else:
            cond_str = f"{left} {op} {right}"
    else:
        cond_str = str(condition)

    cases = inst.get('cases', {})

    true_body = []
    for sub in cases.get('TRUE', []):
        true_body.extend(compile_instruction_go(sub, indent + 1, ctx))

    false_body = []
    for sub in cases.get('FALSE', []):
        false_body.extend(compile_instruction_go(sub, indent + 1, ctx))

    # If the TRUE branch always returns, emit the FALSE body inline at the same
    # indent (Go doesn't need the redundant else). Otherwise emit `} else {`.
    true_returns = any('return' in l for l in true_body)

    lines.append(f"{pad}if {cond_str} {{")
    lines.extend(true_body)
    if false_body and not true_returns:
        lines.append(f"{pad}}} else {{")
        lines.extend(false_body)
        lines.append(f"{pad}}}")
    else:
        lines.append(f"{pad}}}")
        if false_body:
            lines.extend(false_body)

    return lines


# ============================================================================
# TEST GENERATORS — xUnit (C#) and httptest (Go) from (tests ...) section
# ============================================================================

def _test_name_to_ident(name: str) -> str:
    """Normalize a test's quoted name to a valid identifier for C# / Go."""
    import re
    n = str(name).strip('"')
    n = re.sub(r'[^A-Za-z0-9_]', '_', n)
    if n and n[0].isdigit():
        n = '_' + n
    return n or 'unnamed'


def _primary_store_from_bc(bc):
    """Return (store_name, store_def) for the store targeted by RESET_STORE, or first store."""
    if not bc.stores:
        return None, None
    return next(iter(bc.stores.items()))


def _json_literal_from_body_form(body_form: list) -> str:
    """Turn a `['body', ['name', 'Widget'], ['price', 9.99]]` tail into a JSON string literal.

    Values are emitted as-is: numbers stay numeric; non-numeric atoms become JSON strings.
    Nested lists become JSON objects (pairs) or arrays (list of `item` forms).
    The returned string is a JSON document (no surrounding C#/Go quoting)."""
    parts = []
    for pair in body_form:
        if not (isinstance(pair, list) and len(pair) >= 2):
            continue
        key = str(pair[0])
        tail = pair[1:]
        # Nested list/object shape: (key (child …) (child …) …). Multiple `item`
        # children → array; zero or more pair children → object. A single
        # `['item', …]` child is still an array — don't unwrap to a scalar.
        looks_nested = all(isinstance(x, list) for x in tail)
        single_item = (len(tail) == 1 and isinstance(tail[0], list)
                       and tail[0] and tail[0][0] == 'item')
        if looks_nested and (len(tail) > 1 or single_item):
            val = tail
        else:
            val = tail[0]
        parts.append(f'"{key}":{_json_value_literal(val)}')
    return '{' + ','.join(parts) + '}'


def _json_value_literal(val) -> str:
    """Render a single value as a JSON literal.

    Recurses into lists: list of `['item', …]` forms → array; list of
    `[key, value]` pairs → object; scalar → string/number/bool literal.
    """
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if val.lower() in ('true', 'false'):
            return val.lower()
        try:
            float(val)
            return val
        except ValueError:
            pass
        s = val.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{s}"'
    if isinstance(val, list):
        # Empty → empty object by default.
        if not val:
            return '{}'
        # Array shape: list of `['item', …]` forms.
        if all(isinstance(x, list) and x and x[0] == 'item' for x in val):
            elements = [_json_literal_from_body_form(x[1:]) for x in val]
            return '[' + ','.join(elements) + ']'
        # Object shape: list of `[key, value]` pairs.
        return _json_literal_from_body_form(val)
    # Fallback — stringify.
    s = str(val).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{s}"'


def _assert_is_length_op(target: str) -> bool:
    return target.endswith('.length') or target == 'length'


def _cs_seed_value(raw, field_type: str, schemas: dict) -> str:
    """Render a seed_data value as a C# literal, keyed off the schema field type.

    `raw` is a scalar (string/number/bool) or a list of child forms. When the
    schema field is `(list ElemSchema)`, recurse into each `(item …)` child.
    """
    if field_type == 'uuid':
        return f'Guid.Parse("{raw}")'
    if field_type == 'timestamp':
        return f'DateTime.Parse("{raw}")'
    if field_type == 'bool':
        v = str(raw).lower()
        return 'true' if v == 'true' else 'false'
    if field_type == 'int':
        return str(raw)
    if field_type == 'decimal':
        return f'{raw}M'
    if field_type in ('string', 'enum'):
        s = str(raw).strip('"').replace('\\', '\\\\').replace('"', '\\"')
        return f'"{s}"'
    return f'"{raw}"'


def _cs_seed_object(pairs: list, schema_name: str, schemas: dict) -> str:
    """Render a C# object initializer for an entity from a list of (key val…) pairs."""
    schema_def = schemas.get(schema_name, {})
    fields = schema_def.get('fields', {})
    parts = []
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) >= 2):
            continue
        key = str(pair[0])
        fdef = fields.get(key, {})
        ftype = fdef.get('type')
        prop = pascal_case(key)
        # Nested list: collect `(item …)` children → List<Elem> of objects;
        # or bare scalar children → List<scalar> (e.g. `(customer_types
        # wholesale)` or `(customer_types wholesale premium)`).
        if ftype == 'list':
            elem_schema = fdef.get('of') or 'object'
            elem_go_like = ailang_type_to_csharp(elem_schema) if elem_schema in ('string', 'int', 'decimal', 'bool', 'uuid') else elem_schema
            item_forms = [p for p in pair[1:] if isinstance(p, list) and p and p[0] == 'item']
            if item_forms:
                inner = ", ".join(_cs_seed_object(p[1:], elem_schema, schemas) for p in item_forms)
                parts.append(f'{prop} = new List<{elem_schema}> {{ {inner} }}')
            else:
                scalars = [p for p in pair[1:] if not isinstance(p, list)]
                if scalars:
                    vals = ", ".join(_cs_seed_value(s, elem_schema, schemas) for s in scalars)
                    parts.append(f'{prop} = new List<{elem_go_like}> {{ {vals} }}')
                else:
                    parts.append(f'{prop} = new List<{elem_go_like}>()')
            continue
        raw = pair[1]
        parts.append(f'{prop} = {_cs_seed_value(raw, ftype, schemas)}')
    return f'new {schema_name} {{ {", ".join(parts)} }}'


def _go_seed_value(raw, field_type: str, schemas: dict) -> str:
    """Render a seed_data value as a Go literal, keyed off the schema field type.

    Mirrors `_cs_seed_value` but emits Go syntax. UUIDs and timestamps
    are strings in Go (no Guid type — uuid.UUID is aliased to string in
    the generated stores).
    """
    if field_type == 'uuid':
        s = str(raw).strip('"')
        return f'"{s}"'
    if field_type == 'timestamp':
        s = str(raw).strip('"')
        return f'_mustParseTime("{s}")'
    if field_type == 'bool':
        v = str(raw).lower()
        return 'true' if v == 'true' else 'false'
    if field_type == 'int':
        return str(raw)
    if field_type == 'decimal':
        return str(raw)
    if field_type in ('string', 'enum'):
        s = str(raw).strip('"').replace('\\', '\\\\').replace('"', '\\"')
        return f'"{s}"'
    return f'"{raw}"'


def _go_seed_object(pairs: list, schema_name: str, schemas: dict) -> str:
    """Render a Go composite literal for an entity from a list of (key val…) pairs."""
    schema_def = schemas.get(schema_name, {})
    fields = schema_def.get('fields', {})
    parts = []
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) >= 2):
            continue
        key = str(pair[0])
        fdef = fields.get(key, {})
        ftype = fdef.get('type')
        prop = pascal_case(key)
        if ftype == 'list':
            elem_schema = fdef.get('of') or 'object'
            item_forms = [p for p in pair[1:] if isinstance(p, list) and p and p[0] == 'item']
            if item_forms:
                inner = ", ".join(_go_seed_object(p[1:], elem_schema, schemas) for p in item_forms)
                parts.append(f'{prop}: []{elem_schema}{{{inner}}}')
            else:
                scalars = [p for p in pair[1:] if not isinstance(p, list)]
                elem_go = ailang_type_to_go(elem_schema) if elem_schema in ('string', 'int', 'decimal', 'bool', 'uuid') else elem_schema
                if scalars:
                    vals = ", ".join(_go_seed_value(s, elem_schema, schemas) for s in scalars)
                    parts.append(f'{prop}: []{elem_go}{{{vals}}}')
                else:
                    parts.append(f'{prop}: []{elem_go}{{}}')
            continue
        raw = pair[1]
        parts.append(f'{prop}: {_go_seed_value(raw, ftype, schemas)}')
    return f'{schema_name}{{{", ".join(parts)}}}'


def _emit_seed_data_go(seed_op: list, stores: dict, schemas: dict, pad: str) -> list[str]:
    """Emit Go seeding block for one `(seed_data …)` op.

    Reaches the in-memory store via the `_stores` package-level map populated
    by `buildHandler()`, then calls `Seed([]Schema{…})`. Multiple stores in a
    single seed_op are wired sequentially.
    """
    lines = []
    if len(seed_op) < 2:
        return lines
    for store_ref in seed_op[1:]:
        if not (isinstance(store_ref, list) and store_ref):
            continue
        store_name = str(store_ref[0])
        store_def = stores.get(store_name)
        if not store_def:
            continue
        schema = store_def['schema']
        items = [p for p in store_ref[1:] if isinstance(p, list) and p and p[0] == 'item']
        if not items:
            continue
        entities = [_go_seed_object(it[1:], schema, schemas) for it in items]
        inner = ", ".join(entities)
        lines.append(f'{pad}_stores["{schema}"].(*{schema}Store).Seed([]{schema}{{{inner}}})')
    return lines


def _emit_seed_data_csharp(seed_op: list, stores: dict, schemas: dict, pad: str, factory_var: str) -> list[str]:
    """Emit a seeding block for one `(seed_data …)` op.

    `seed_op` is the full `['seed_data', [store_name, ['item', …], …]]` form.
    For each store reference, resolve the store service via DI and call Seed()
    with a C# array of schema-typed initializers.
    """
    lines = []
    if len(seed_op) < 2:
        return lines
    lines.append(f'{pad}{{')
    lines.append(f'{pad}    using var _seedScope = {factory_var}.Services.CreateScope();')
    for store_ref in seed_op[1:]:
        if not (isinstance(store_ref, list) and store_ref):
            continue
        store_name = str(store_ref[0])
        store_def = stores.get(store_name)
        if not store_def:
            continue
        schema = store_def.get('schema')
        items = [p for p in store_ref[1:] if isinstance(p, list) and p and p[0] == 'item']
        if not items:
            continue
        local = '_' + camel_case(store_name)
        lines.append(f'{pad}    var {local} = _seedScope.ServiceProvider.GetRequiredService<I{schema}Store>();')
        entities = [_cs_seed_object(it[1:], schema, schemas) for it in items]
        inner = ", ".join(entities)
        lines.append(f'{pad}    {local}.Seed(new[] {{ {inner} }});')
    lines.append(f'{pad}}}')
    return lines


def _split_json_path(target: str) -> list[str]:
    """'body.error.type' → ['error', 'type']. Strips the leading 'body.'."""
    segs = target.split('.')
    if segs and segs[0] == 'body':
        segs = segs[1:]
    return segs


def _cs_json_walk(segs: list[str]) -> str:
    """Render a `JsonElement` walk expression from a split path.

    Segments of the form `name[N]` become `.GetProperty("name")[N]` so
    assertions on `discounts_applied[0].type` reach the right subtree.
    Bare names become `.GetProperty("name")`. Returns the expression
    starting from `_body`.
    """
    import re
    walk = '_body'
    for s in segs:
        m = re.match(r'^([^\[]+)\[(\d+)\]$', s)
        if m:
            walk += f'.GetProperty("{m.group(1)}")[{m.group(2)}]'
        else:
            walk += f'.GetProperty("{s}")'
    return walk


def _interpolate_path_csharp(path: str, captures: set) -> str:
    """Replace `{var}` in a path with `{capVar}` (C# interpolated-string placeholders),
    but only when `var` was previously CAPTURE-d. Returns a C#-quoted expression."""
    import re
    used = []
    def repl(m):
        v = m.group(1)
        if v in captures:
            used.append(v)
            return '{' + v + '}'
        return m.group(0)
    new_path = re.sub(r'\{(\w+)\}', repl, path)
    # If no interpolation, emit as plain string. Otherwise as $"...".
    if used:
        return '$"' + new_path + '"'
    return '"' + new_path + '"'


def _go_walk_body(segs: list[str]) -> tuple[str, str]:
    """Walk a split body path (e.g. ['items', 'length']) into Go expressions.

    Mirrors `_cs_json_walk` but adapted to Go's `map[string]any` + `[]any`
    decoding from `encoding/json`. A segment like `discounts_applied[0]`
    becomes `(_body["discounts_applied"].([]any)[0].(map[string]any))`.

    Returns (walk_expr_to_parent_map, last_segment_key) so callers can do
    `{walk}["{last}"]` uniformly.
    """
    import re
    walk = '_body'
    for s in segs[:-1]:
        m = re.match(r'^([^\[]+)\[(\d+)\]$', s)
        if m:
            name, idx = m.group(1), m.group(2)
            walk = f'({walk}["{name}"].([]any)[{idx}].(map[string]any))'
        else:
            walk = f'({walk}["{s}"].(map[string]any))'
    last = segs[-1]
    return walk, last


def _interpolate_path_go(path: str, captures: set):
    """Return (fmt_string, args) for fmt.Sprintf; or (literal, None) if no captures."""
    import re
    used = []
    def repl(m):
        v = m.group(1)
        if v in captures:
            used.append(v)
            return '%s'
        return m.group(0)
    new_path = re.sub(r'\{(\w+)\}', repl, path)
    if used:
        return new_path, used
    return new_path, None


def _group_ops_by_request(run_ops: list) -> list[list]:
    """Group (run ...) ops into per-REQUEST steps: each REQUEST begins a new step,
    and all following ASSERT/CAPTURE ops attach to it. A leading block without a
    REQUEST stays empty (unusual but tolerated)."""
    groups: list[list] = []
    current: list = []
    for op in run_ops:
        if not (isinstance(op, list) and op):
            continue
        if op[0] == 'REQUEST':
            if current:
                groups.append(current)
            current = [op]
        else:
            current.append(op)
    if current:
        groups.append(current)
    return groups


def _env_var_for_dependency(bc, dep_name: str, fallback: str = None) -> str:
    """Resolve `(dependencies (name (base_url_ref ENV.X) …))` → env var name `X`.
    Falls back to `UPPER_SNAKE_URL` when no explicit ref exists."""
    dep = (bc.dependencies or {}).get(dep_name) or {}
    ref = dep.get('base_url_ref', '') or ''
    ref = ref.replace('ENV.', '').strip()
    if ref:
        return ref
    if fallback:
        return fallback
    return dep_name.upper() + "_URL"


def _mock_body_to_json(body) -> str:
    """Turn a parsed (body …) tail into a JSON literal string.

    - List of `['item', …]` forms → JSON array (each item's children are
      (key value) pairs emitted as an object).
    - List of `(key value)` pairs → JSON object.
    - None / empty → `"[]"`. Empty mock bodies are most commonly list-endpoint
      responses (GET /products, /orders, /payments); defaulting to an empty
      array lets downstream handlers that call `AsArray()`/ITERATE not crash.
      Tests that need an empty object can write `(body)` → currently parses
      as empty list, same result; or explicit scalars / pair-lists still
      produce an object.
    """
    if not body:
        return "[]"
    # Detect array shape: any top-level 'item' form marks this body as an array.
    if any(isinstance(b, list) and b and b[0] == 'item' for b in body):
        elements = []
        for b in body:
            if isinstance(b, list) and b and b[0] == 'item':
                elements.append(_json_literal_from_body_form(b[1:]))
        return '[' + ','.join(elements) + ']'
    return _json_literal_from_body_form(body)


def _scan_captures(run_ops: list) -> list[str]:
    """Return CAPTURE target variables in declaration order (dedup preserving order)."""
    seen = []
    out = []
    for op in run_ops:
        if isinstance(op, list) and op and op[0] == 'CAPTURE':
            var = str(op[-1])
            if var not in seen:
                seen.append(var)
                out.append(var)
    return out


def _compile_test_run_csharp(run_ops: list, captures: set, indent: int) -> list[str]:
    """Compile a test's `run` sequence to C# statements.

    Each REQUEST step is wrapped in its own `{ }` block to avoid redeclaring
    `_req`/`_resp`/`_body` locals. CAPTURE targets are hoisted out so they flow
    across blocks.
    """
    pad = "    " * indent
    lines = []

    # Hoist CAPTURE targets to the outer test scope.
    for var in _scan_captures(run_ops):
        lines.append(f'{pad}string? {var} = null;')
        captures.add(var)

    inner_pad = pad + "    "
    for step in _group_ops_by_request(run_ops):
        if not step:
            continue
        if step[0][0] != 'REQUEST':
            # Standalone ASSERT/CAPTURE with no request — unlikely, but handle.
            lines.extend(_compile_test_step_body_csharp(step, captures, inner_pad))
            continue
        lines.append(f'{pad}{{')
        lines.extend(_compile_test_step_body_csharp(step, captures, inner_pad))
        lines.append(f'{pad}}}')
    return lines


def _compile_test_step_body_csharp(step: list, captures: set, pad: str) -> list[str]:
    """Emit the body of a single REQUEST-step block (the REQUEST + its trailing asserts)."""
    lines = []
    req_op = step[0]
    method = str(req_op[1]).upper()
    path = str(req_op[2]).strip('"')
    auth = False
    body_form = None
    for sub in req_op[3:]:
        if isinstance(sub, list) and sub:
            if sub[0] == 'auth':
                auth = str(sub[1]).lower() == 'true'
            elif sub[0] == 'body':
                body_form = sub[1:]
    url_expr = _interpolate_path_csharp(path, captures)
    method_enum = {
        'GET': 'Get', 'POST': 'Post', 'PUT': 'Put',
        'DELETE': 'Delete', 'PATCH': 'Patch', 'HEAD': 'Head',
    }.get(method, 'Get')
    lines.append(f'{pad}var _req = new HttpRequestMessage(HttpMethod.{method_enum}, {url_expr});')
    if auth:
        lines.append(f'{pad}_req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", TestToken());')
    if body_form:
        json_str = _json_literal_from_body_form(body_form).replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{pad}_req.Content = new StringContent("{json_str}", Encoding.UTF8, "application/json");')
    lines.append(f'{pad}var _resp = await _client.SendAsync(_req);')
    lines.append(f'{pad}var _status = (int)_resp.StatusCode;')
    lines.append(f'{pad}var _bodyStr = await _resp.Content.ReadAsStringAsync();')
    lines.append(f'{pad}JsonElement _body = default;')
    lines.append(f'{pad}if (!string.IsNullOrWhiteSpace(_bodyStr)) {{ _body = JsonDocument.Parse(_bodyStr).RootElement; }}')

    for op in step[1:]:
        head = op[0]
        if head == 'ASSERT':
            target = str(op[1])
            if target == 'status':
                want = op[2]
                lines.append(f'{pad}Assert.Equal({want}, _status);')
            elif _assert_is_length_op(target):
                if len(op) >= 4:
                    cmp_op, val = str(op[2]), op[3]
                else:
                    cmp_op, val = '==', op[2]
                cs_cmp = {'==': '==', '!=': '!=', '<': '<', '<=': '<=', '>': '>', '>=': '>='}.get(cmp_op, '==')
                # Walk to the enclosing array: `body.items.length` →
                # `_body.GetProperty("items").GetArrayLength()`. Bare `body.length`
                # / `length` stays on the root `_body`.
                segs = _split_json_path(target)
                if segs and segs[-1] == 'length':
                    segs = segs[:-1]
                walk = _cs_json_walk(segs)
                lines.append(f'{pad}Assert.True({walk}.GetArrayLength() {cs_cmp} {val}, $"{target} = {{{walk}.GetArrayLength()}}");')
            else:
                segs = _split_json_path(target)
                walk = _cs_json_walk(segs)
                expected = op[2] if len(op) > 2 else None
                expected_str = str(expected).strip('"').lower() if expected is not None else ''
                if str(expected) == 'exists':
                    probe = _cs_json_walk(segs[:-1])
                    last = segs[-1] if segs else ''
                    lines.append(f'{pad}Assert.True({probe}.TryGetProperty("{last}", out _));')
                elif isinstance(expected, bool) or expected_str in ('true', 'false'):
                    want_bool = 'true' if (expected is True or expected_str == 'true') else 'false'
                    lines.append(f'{pad}Assert.Equal({want_bool}, {walk}.GetBoolean());')
                elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
                    lines.append(f'{pad}Assert.Equal({expected}M, {walk}.GetDecimal());')
                else:
                    val_str = str(expected).strip('"')
                    lines.append(f'{pad}Assert.Equal("{val_str}", {walk}.GetString());')
        elif head == 'CAPTURE':
            target = str(op[1])
            var = str(op[-1])
            segs = _split_json_path(target)
            walk = '_body'
            for s in segs:
                walk += f'.GetProperty("{s}")'
            # Assign to the hoisted outer variable (no `var` prefix).
            lines.append(f'{pad}{var} = {walk}.GetString();')
    return lines


def _compile_test_run_go(run_ops: list, captures: set, indent: int) -> list[str]:
    """Compile a test's `run` sequence to Go statements inside a *testing.T.

    Each REQUEST step lives in its own `{ ... }` block so `_req`/`_resp`/`_body`
    stay redeclarable. CAPTURE targets are hoisted to the outer scope.
    """
    pad = "\t" * indent
    lines = []

    cap_vars = _scan_captures(run_ops)
    for var in cap_vars:
        lines.append(f'{pad}var {var} string')
        captures.add(var)
    if cap_vars:
        # Keep Go happy if a capture is never read.
        refs = '; '.join(f'_ = {v}' for v in cap_vars)
        lines.append(f'{pad}defer func() {{ {refs} }}()')

    inner_pad = pad + "\t"
    for step in _group_ops_by_request(run_ops):
        if not step:
            continue
        if step[0][0] != 'REQUEST':
            lines.extend(_compile_test_step_body_go(step, captures, inner_pad))
            continue
        lines.append(f'{pad}{{')
        lines.extend(_compile_test_step_body_go(step, captures, inner_pad))
        lines.append(f'{pad}}}')
    return lines


def _compile_test_step_body_go(step: list, captures: set, pad: str) -> list[str]:
    lines = []
    req_op = step[0]
    method = str(req_op[1]).upper()
    path = str(req_op[2]).strip('"')
    auth = False
    body_form = None
    for sub in req_op[3:]:
        if isinstance(sub, list) and sub:
            if sub[0] == 'auth':
                auth = str(sub[1]).lower() == 'true'
            elif sub[0] == 'body':
                body_form = sub[1:]
    fmt_path, args = _interpolate_path_go(path, captures)
    if args:
        args_str = ', '.join(args)
        lines.append(f'{pad}_url := ts.URL + fmt.Sprintf("{fmt_path}", {args_str})')
    else:
        lines.append(f'{pad}_url := ts.URL + "{fmt_path}"')
    if body_form:
        json_str = _json_literal_from_body_form(body_form)
        lines.append(f'{pad}_reqBody := strings.NewReader(`{json_str}`)')
        lines.append(f'{pad}_req, _ := http.NewRequest("{method}", _url, _reqBody)')
        lines.append(f'{pad}_req.Header.Set("Content-Type", "application/json")')
    else:
        lines.append(f'{pad}_req, _ := http.NewRequest("{method}", _url, nil)')
    if auth:
        lines.append(f'{pad}_req.Header.Set("Authorization", "Bearer testtoken")')
    lines.append(f'{pad}_resp, _err := ts.Client().Do(_req)')
    lines.append(f'{pad}if _err != nil {{ t.Fatalf("request failed: %v", _err) }}')
    lines.append(f'{pad}_status := _resp.StatusCode')
    lines.append(f'{pad}_rawBody, _ := io.ReadAll(_resp.Body)')
    lines.append(f'{pad}_resp.Body.Close()')
    lines.append(f'{pad}var _body map[string]any')
    lines.append(f'{pad}var _bodyArr []any')
    lines.append(f'{pad}if len(_rawBody) > 0 {{')
    lines.append(f"{pad}\tif _rawBody[0] == '[' {{ _ = json.Unmarshal(_rawBody, &_bodyArr) }} else {{ _ = json.Unmarshal(_rawBody, &_body) }}")
    lines.append(f'{pad}}}')
    lines.append(f'{pad}_ = _body; _ = _bodyArr; _ = _status; _ = _rawBody')

    for op in step[1:]:
        head = op[0]
        if head == 'ASSERT':
            target = str(op[1])
            if target == 'status':
                want = op[2]
                lines.append(f'{pad}if _status != {want} {{ t.Fatalf("want status {want}, got %d (body=%s)", _status, string(_rawBody)) }}')
            elif _assert_is_length_op(target):
                cmp_op, val = ('==', op[2]) if len(op) < 4 else (str(op[2]), op[3])
                # `body.length` → len(_bodyArr) (root is an array).
                # `body.items.length` → len of the nested slice, reached via
                # map indexing and a []any assertion.
                segs = _split_json_path(target)
                if segs and segs[-1] == 'length':
                    segs = segs[:-1]
                if not segs:
                    lines.append(f'{pad}if !(len(_bodyArr) {cmp_op} {val}) {{ t.Errorf("{target} = %d, want {cmp_op} {val}", len(_bodyArr)) }}')
                else:
                    walk, last = _go_walk_body(segs)
                    lines.append(f'{pad}if _arr, _ := {walk}["{last}"].([]any); !(len(_arr) {cmp_op} {val}) {{ t.Errorf("{target} = %d, want {cmp_op} {val}", len(_arr)) }}')
            else:
                segs = _split_json_path(target)
                walk, last = _go_walk_body(segs)
                expected = op[2] if len(op) > 2 else None
                expected_str = str(expected).strip('"').lower() if expected is not None else ''
                if str(expected) == 'exists':
                    lines.append(f'{pad}if _, _ok := {walk}["{last}"]; !_ok {{ t.Errorf("missing field: {target}") }}')
                elif isinstance(expected, bool) or expected_str in ('true', 'false'):
                    want_bool = 'true' if (expected is True or expected_str == 'true') else 'false'
                    lines.append(f'{pad}if v, _ := {walk}["{last}"].(bool); v != {want_bool} {{ t.Errorf("{target} = %v, want {want_bool}", {walk}["{last}"]) }}')
                elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
                    lines.append(f'{pad}if v, _ := {walk}["{last}"].(float64); v != {float(expected)} {{ t.Errorf("{target} = %v, want {expected}", {walk}["{last}"]) }}')
                else:
                    val_str = str(expected).strip('"')
                    lines.append(f'{pad}if v, _ := {walk}["{last}"].(string); v != "{val_str}" {{ t.Errorf("{target} = %v, want {val_str}", {walk}["{last}"]) }}')
        elif head == 'CAPTURE':
            target = str(op[1])
            var = str(op[-1])
            segs = _split_json_path(target)
            walk, last = _go_walk_body(segs)
            # Assign to the hoisted outer variable (type already declared as string).
            lines.append(f'{pad}if _v, _ok := {walk}["{last}"].(string); _ok {{ {var} = _v }}')
    return lines


def generate_csharp_pg_tests(bc, main_output_dir: str, pg_stores: dict):
    """Emit an xUnit test project backed by a Testcontainers PostgreSQL instance.

    Layout:
      - `PgFixture.cs`: ICollectionFixture starting a `postgres:16-alpine`
        container once per test collection; runs the CREATE TABLE DDL against
        it and hands connection info to tests. Sets `DB_CONNECTION_STRING` so
        the service under test (Program.cs) reads the same container.
      - `ApiTests.cs`: `[Collection("pg")]` tests that get the fixture by DI,
        TRUNCATE before and after each test, honor `(SQL_SEED store "INSERT …")`
        and `(RESET_STORE store)` via raw Npgsql commands.

    Requires Docker at test time. Build passes without Docker; only `dotnet test`
    actually needs the daemon.
    """
    service_name = bc.service['name']
    main_proj_name = pascal_case(service_name)
    tests_dir = os.path.join(main_output_dir, "Tests")
    os.makedirs(tests_dir, exist_ok=True)

    # Only one PG store per service is supported for now (the 4 legacy services
    # that hit this path all use a single primary store). Mirror the Go side.
    primary_store_name, primary_store_def = next(iter(pg_stores.items()))
    primary_schema = primary_store_def['schema']
    primary_table = primary_store_def.get('table') or primary_schema.lower() + 's'
    ddl = generate_postgres_ddl(primary_store_def)

    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        '    <IsPackable>false</IsPackable>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <PackageReference Include="Microsoft.AspNetCore.Mvc.Testing" Version="10.0.0" />\n'
        '    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.12.0" />\n'
        '    <PackageReference Include="xunit" Version="2.9.2" />\n'
        '    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />\n'
        '    <PackageReference Include="Testcontainers.PostgreSql" Version="4.7.0" />\n'
        '    <PackageReference Include="Npgsql" Version="10.0.0" />\n'
        '  </ItemGroup>\n'
        '  <ItemGroup>\n'
        f'    <ProjectReference Include="..\\{main_proj_name}.csproj" />\n'
        '  </ItemGroup>\n'
        '</Project>'
    )
    with open(os.path.join(tests_dir, f"{main_proj_name}.Tests.csproj"), 'w') as f:
        f.write(csproj)

    # ── PgFixture.cs — Collection-scoped container ──
    fixture = []
    fixture.append("// ═══════════════════════════════════════════════════════════════")
    fixture.append("// AILang Transpiler Output — Testcontainers PG fixture")
    fixture.append("// Auto-generated — DO NOT EDIT")
    fixture.append("// ═══════════════════════════════════════════════════════════════")
    fixture.append("using Npgsql;")
    fixture.append("using Testcontainers.PostgreSql;")
    fixture.append("using Xunit;")
    fixture.append("")
    fixture.append("public sealed class PgFixture : IAsyncLifetime")
    fixture.append("{")
    fixture.append("    // postgres:16-alpine keeps pulls small and matches production Alpine images.")
    fixture.append("    private readonly PostgreSqlContainer _container = new PostgreSqlBuilder()")
    fixture.append('        .WithImage("postgres:16-alpine")')
    fixture.append('        .WithDatabase("ailang_test")')
    fixture.append('        .WithUsername("postgres")')
    fixture.append('        .WithPassword("postgres")')
    fixture.append("        .Build();")
    fixture.append("")
    fixture.append("    public string ConnectionString => _container.GetConnectionString();")
    fixture.append("")
    fixture.append(f'    private const string Ddl = @"{ddl.replace(chr(34), chr(34)*2)}";')
    fixture.append("")
    fixture.append("    public async Task InitializeAsync()")
    fixture.append("    {")
    fixture.append("        await _container.StartAsync();")
    fixture.append('        // Program.cs reads DB_CONNECTION_STRING at WebApplicationFactory bootstrap;')
    fixture.append('        // setting it here (before any test builds a factory) routes every')
    fixture.append('        // Npgsql call at the shared container.')
    fixture.append(f'        Environment.SetEnvironmentVariable("DB_CONNECTION_STRING", ConnectionString);')
    fixture.append("        await using var conn = new NpgsqlConnection(ConnectionString);")
    fixture.append("        await conn.OpenAsync();")
    fixture.append("        await using var cmd = new NpgsqlCommand(Ddl, conn);")
    fixture.append("        await cmd.ExecuteNonQueryAsync();")
    fixture.append("    }")
    fixture.append("")
    fixture.append("    public async Task DisposeAsync()")
    fixture.append("    {")
    fixture.append("        await _container.DisposeAsync();")
    fixture.append("    }")
    fixture.append("")
    fixture.append("    public async Task ExecAsync(string sql)")
    fixture.append("    {")
    fixture.append("        await using var conn = new NpgsqlConnection(ConnectionString);")
    fixture.append("        await conn.OpenAsync();")
    fixture.append("        await using var cmd = new NpgsqlCommand(sql, conn);")
    fixture.append("        await cmd.ExecuteNonQueryAsync();")
    fixture.append("    }")
    fixture.append("}")
    fixture.append("")
    fixture.append('[CollectionDefinition("pg")]')
    fixture.append("public class PgCollection : ICollectionFixture<PgFixture> { }")
    with open(os.path.join(tests_dir, "PgFixture.cs"), 'w') as f:
        f.write("\n".join(fixture))

    # ── ApiTests.cs ──
    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append(f"// AILang Transpiler Output — xUnit Tests (PG) for {service_name}")
    lines.append("// Auto-generated from (tests ...) section — DO NOT EDIT")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("using System.IdentityModel.Tokens.Jwt;")
    lines.append("using System.Net.Http;")
    lines.append("using System.Net.Http.Headers;")
    lines.append("using System.Security.Claims;")
    lines.append("using System.Text;")
    lines.append("using System.Text.Json;")
    lines.append("using Microsoft.AspNetCore.Mvc.Testing;")
    lines.append("using Microsoft.IdentityModel.Tokens;")
    lines.append("using Xunit;")
    lines.append("")
    lines.append('[Collection("pg")]')
    lines.append("public class ApiTests : IAsyncLifetime")
    lines.append("{")
    lines.append("    private readonly PgFixture _pg;")
    lines.append("    private readonly WebApplicationFactory<Program> _factory;")
    lines.append("    private readonly HttpClient _client;")
    lines.append("")
    lines.append("    public ApiTests(PgFixture pg)")
    lines.append("    {")
    lines.append("        _pg = pg;")
    lines.append("        _factory = new WebApplicationFactory<Program>();")
    lines.append("        _client = _factory.CreateClient();")
    lines.append("    }")
    lines.append("")
    lines.append("    public Task InitializeAsync() => Task.CompletedTask;")
    lines.append("    public async Task DisposeAsync()")
    lines.append("    {")
    lines.append("        _client.Dispose();")
    lines.append("        await _factory.DisposeAsync();")
    lines.append("    }")
    lines.append("")
    lines.append("    private static string TestToken()")
    lines.append("    {")
    lines.append('        var secret = Environment.GetEnvironmentVariable("JWT_SECRET") ?? "dev-secret-key-min-32-characters!!";')
    lines.append("        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));")
    lines.append("        var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);")
    lines.append("        var token = new JwtSecurityToken(")
    lines.append('            claims: new[] { new Claim("sub", "test-user") },')
    lines.append("            signingCredentials: creds);")
    lines.append("        return new JwtSecurityTokenHandler().WriteToken(token);")
    lines.append("    }")
    lines.append("")

    for test in bc.tests:
        ident = _test_name_to_ident(test['name'])
        run_ops = test.get('run') or []
        setup_ops = test.get('setup_ops') or []
        clean_ops = test.get('clean') or []
        captures: set = set()
        lines.append("    [Fact]")
        lines.append(f"    public async Task {ident}()")
        lines.append("    {")
        # Setup ops — RESET_STORE / SQL_SEED translate to raw SQL against the fixture.
        # Multi-line SQL from the bytecode (INSERT INTO … VALUES …) requires C#
        # verbatim strings so embedded newlines stay legal.
        for op in setup_ops:
            if not (isinstance(op, list) and op):
                continue
            head = str(op[0]).upper()
            if head == 'RESET_STORE':
                lines.append(f'        await _pg.ExecAsync(@"TRUNCATE TABLE ""{primary_table}"" CASCADE");')
            elif head == 'SQL_SEED':
                raw_sql = str(op[2]) if len(op) > 2 else ''
                safe = raw_sql.replace('"', '""')
                lines.append(f'        await _pg.ExecAsync(@"{safe}");')
        lines.append("        try")
        lines.append("        {")
        lines.extend(_compile_test_run_csharp(run_ops, captures, indent=3))
        lines.append("        }")
        lines.append("        finally")
        lines.append("        {")
        # Clean ops — same translation as setup but in the finally.
        for op in clean_ops:
            if isinstance(op, str):
                # Legacy string form (e.g. "RESET_STORE(product_store)") — normalize.
                if op.strip().upper().startswith('RESET_STORE'):
                    lines.append(f'            await _pg.ExecAsync(@"TRUNCATE TABLE ""{primary_table}"" CASCADE");')
                continue
            if not (isinstance(op, list) and op):
                continue
            head = str(op[0]).upper()
            if head == 'RESET_STORE':
                lines.append(f'            await _pg.ExecAsync(@"TRUNCATE TABLE ""{primary_table}"" CASCADE");')
            elif head == 'SQL_SEED':
                raw_sql = str(op[2]) if len(op) > 2 else ''
                safe = raw_sql.replace('"', '""')
                lines.append(f'            await _pg.ExecAsync(@"{safe}");')
        lines.append("        }")
        lines.append("    }")
        lines.append("")

    lines.append("}")

    with open(os.path.join(tests_dir, "ApiTests.cs"), 'w') as f:
        f.write("\n".join(lines))

    print(f"  ✓ C# xUnit (PG/Testcontainers) tests generated in {tests_dir}/")


def generate_csharp_tests(bc, main_output_dir: str):
    """Emit an xUnit test project that exercises the generated C# API.

    Structure: sibling `Tests/` folder inside `main_output_dir`. The test project
    references the main .csproj by relative path. One `[Fact]` per `(test ...)`.
    """
    if not bc.tests:
        return
    # Postgres-backed services run against a real PG provisioned via Testcontainers.
    pg_stores = {n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'}
    if pg_stores:
        generate_csharp_pg_tests(bc, main_output_dir, pg_stores)
        return

    service_name = bc.service['name']
    main_proj_name = pascal_case(service_name)
    tests_dir = os.path.join(main_output_dir, "Tests")
    os.makedirs(tests_dir, exist_ok=True)

    # Primary in-memory store — used by the RESET_STORE in (clean ...).
    primary_store_name, primary_store_def = _primary_store_from_bc(bc)
    primary_schema = primary_store_def['schema'] if primary_store_def else None

    # .csproj
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        '    <IsPackable>false</IsPackable>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <PackageReference Include="Microsoft.AspNetCore.Mvc.Testing" Version="10.0.0" />\n'
        '    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.12.0" />\n'
        '    <PackageReference Include="xunit" Version="2.9.2" />\n'
        '    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />\n'
        '  </ItemGroup>\n'
        '  <ItemGroup>\n'
        f'    <ProjectReference Include="..\\{main_proj_name}.csproj" />\n'
        '  </ItemGroup>\n'
        '</Project>'
    )
    with open(os.path.join(tests_dir, f"{main_proj_name}.Tests.csproj"), 'w') as f:
        f.write(csproj)

    # Any test in this file that needs CALL_SERVICE mocks drives an extra helper
    # + per-test WithWebHostBuilder override. Emit those conditionally.
    any_mocks = any(t.get('mock_services') for t in bc.tests)

    # ApiTests.cs
    jwt_env_default = "dev-secret-key-min-32-characters!!"
    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append(f"// AILang Transpiler Output — C# xUnit Tests for {service_name}")
    lines.append("// Auto-generated from (tests ...) section — DO NOT EDIT")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("using System.IdentityModel.Tokens.Jwt;")
    lines.append("using System.Net;")
    lines.append("using System.Net.Http;")
    lines.append("using System.Net.Http.Headers;")
    lines.append("using System.Security.Claims;")
    lines.append("using System.Text;")
    lines.append("using System.Text.Json;")
    lines.append("using Microsoft.AspNetCore.Mvc.Testing;")
    lines.append("using Microsoft.AspNetCore.TestHost;")
    lines.append("using Microsoft.Extensions.DependencyInjection;")
    lines.append("using Microsoft.Extensions.DependencyInjection.Extensions;")
    lines.append("using Microsoft.Extensions.Http;")
    lines.append("using Microsoft.IdentityModel.Tokens;")
    lines.append("using Xunit;")
    lines.append("")
    if any_mocks:
        # MockHttpHandler: intercepts CALL_SERVICE outbound traffic for a named HttpClient.
        # First matching (method, path) rule wins; unmatched requests respond 404.
        # We install it via ConfigurePrimaryHttpMessageHandler so the resilience pipeline
        # configured in Program.cs still runs on top of the canned transport.
        lines.append("internal sealed class MockHttpHandler : HttpMessageHandler")
        lines.append("{")
        lines.append("    private readonly (string Method, string Path, int DelayMs, int Status, string Body)[] _rules;")
        lines.append("    public MockHttpHandler((string, string, int, int, string)[] rules) { _rules = rules; }")
        lines.append("    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)")
        lines.append("    {")
        lines.append("        var method = request.Method.Method;")
        lines.append("        var path = request.RequestUri!.AbsolutePath;")
        lines.append("        foreach (var rule in _rules)")
        lines.append("        {")
        lines.append("            if (rule.Method == method && rule.Path == path)")
        lines.append("            {")
        lines.append("                if (rule.DelayMs > 0) await Task.Delay(rule.DelayMs, cancellationToken);")
        lines.append("                var resp = new HttpResponseMessage((HttpStatusCode)rule.Status);")
        lines.append('                resp.Content = new StringContent(rule.Body, Encoding.UTF8, "application/json");')
        lines.append("                return resp;")
        lines.append("            }")
        lines.append("        }")
        lines.append("        return new HttpResponseMessage(HttpStatusCode.NotFound);")
        lines.append("    }")
        lines.append("}")
        lines.append("")
    lines.append("public class ApiTests : IDisposable")
    lines.append("{")
    lines.append("    // A fresh WebApplicationFactory per test: each test gets its own DI graph")
    lines.append("    // and in-memory store, so RESET_STORE in (clean ...) is mostly a safety net.")
    lines.append("    private readonly WebApplicationFactory<Program> _factory = new();")
    lines.append("    private readonly HttpClient _client;")
    lines.append("")
    lines.append("    public ApiTests()")
    lines.append("    {")
    lines.append("        _client = _factory.CreateClient();")
    lines.append("    }")
    lines.append("")
    lines.append("    public void Dispose()")
    lines.append("    {")
    lines.append("        _client.Dispose();")
    lines.append("        _factory.Dispose();")
    lines.append("    }")
    lines.append("")
    lines.append("    // Mints a JWT signed with the dev secret. The server disables issuer/audience/lifetime")
    lines.append("    // validation, so only the HMAC signature matters.")
    lines.append("    private static string TestToken()")
    lines.append("    {")
    lines.append(f'        var secret = Environment.GetEnvironmentVariable("JWT_SECRET") ?? "{jwt_env_default}";')
    lines.append("        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(secret));")
    lines.append("        var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);")
    lines.append("        var token = new JwtSecurityToken(")
    lines.append('            claims: new[] { new Claim("sub", "test-user") },')
    lines.append("            signingCredentials: creds);")
    lines.append("        return new JwtSecurityTokenHandler().WriteToken(token);")
    lines.append("    }")
    lines.append("")
    if primary_schema:
        lines.append(f"    private void ResetStore()")
        lines.append("    {")
        lines.append("        using var scope = _factory.Services.CreateScope();")
        lines.append(f"        var svc = scope.ServiceProvider.GetRequiredService<I{primary_schema}Store>();")
        lines.append("        svc.Reset();")
        lines.append("    }")
        lines.append("")

    for test in bc.tests:
        ident = _test_name_to_ident(test['name'])
        run_ops = test.get('run') or []
        mocks = test.get('mock_services') or {}
        setup_ops = test.get('setup_ops') or []
        override_time_ops = [op for op in setup_ops if isinstance(op, list) and op and op[0] == 'override_time']
        captures: set = set()
        lines.append("    [Fact]")
        lines.append(f"    public async Task {ident}()")
        lines.append("    {")
        client_var = "_client"
        # Build a derived factory when the test declares mocks or needs a
        # DI-level override (override_time → FakeAiLangClock). Everything
        # funnels through a single ConfigureTestServices block.
        needs_factory = bool(mocks) or bool(override_time_ops)
        if needs_factory:
            lines.append("        var _mockFactory = _factory.WithWebHostBuilder(builder =>")
            lines.append("        {")
            lines.append("            builder.ConfigureTestServices(services =>")
            lines.append("            {")
            for svc, endpoints in mocks.items():
                rules_cs = []
                for ep in endpoints:
                    body_json = _mock_body_to_json(ep.get('body'))
                    body_escaped = body_json.replace('\\', '\\\\').replace('"', '\\"')
                    delay_ms = ep.get('delay_ms', 0) or 0
                    rules_cs.append(f'("{ep["method"]}", "{ep["path"]}", {delay_ms}, {ep["status"]}, "{body_escaped}")')
                rules_join = ",\n                        ".join(rules_cs)
                lines.append(f'                services.AddHttpClient("{svc}")')
                lines.append("                    .ConfigurePrimaryHttpMessageHandler(() => new MockHttpHandler(new (string, string, int, int, string)[]")
                lines.append("                    {")
                lines.append("                        " + rules_join)
                lines.append("                    }));")
                lines.append(f'                services.AddHttpClient("{svc}_no_retry")')
                lines.append("                    .ConfigurePrimaryHttpMessageHandler(() => new MockHttpHandler(new (string, string, int, int, string)[]")
                lines.append("                    {")
                lines.append("                        " + rules_join)
                lines.append("                    }));")
            for ov in override_time_ops:
                # `(override_time (day_of_week tuesday))` or `(now "ISO-8601")`.
                # Replace IAiLangClock with a FakeAiLangClock configured from
                # the setup form.
                fake_init_parts = []
                for spec in ov[1:]:
                    if not (isinstance(spec, list) and len(spec) >= 2):
                        continue
                    key = str(spec[0])
                    val = spec[1]
                    if key == 'day_of_week':
                        fake_init_parts.append(f'FixedDayOfWeek = "{val}"')
                    elif key == 'now':
                        fake_init_parts.append(f'FixedNow = DateTime.Parse("{val}")')
                init = ", ".join(fake_init_parts)
                lines.append("                services.RemoveAll<IAiLangClock>();")
                lines.append(f"                services.AddSingleton<IAiLangClock>(new FakeAiLangClock {{ {init} }});")
            lines.append("            });")
            lines.append("        });")
            lines.append("        using var _mockedClient = _mockFactory.CreateClient();")
            client_var = "_mockedClient"
        # Swap the effective client name used by the compiled run ops.
        if client_var != "_client":
            lines.append(f"        var _client = {client_var};  // shadow outer field for this test")
        # Emit setup_ops (seed_data, force_circuit_state) before the try
        # block so they happen on the same factory that serves the test's
        # HTTP requests. override_time was already wired into the derived
        # factory above.
        seed_factory = '_mockFactory' if needs_factory else '_factory'
        for setup_op in (test.get('setup_ops') or []):
            if isinstance(setup_op, list) and setup_op and setup_op[0] == 'seed_data':
                lines.extend(_emit_seed_data_csharp(setup_op, bc.stores, bc.schemas,
                                                     pad='        ', factory_var=seed_factory))
            elif isinstance(setup_op, list) and setup_op and setup_op[0] == 'force_circuit_state':
                # `(force_circuit_state (<policy_name> OPEN))` — isolate the
                # ManualControl keyed by policy so Polly throws
                # BrokenCircuitException on the next call.
                for spec in setup_op[1:]:
                    if not (isinstance(spec, list) and len(spec) >= 2):
                        continue
                    policy_name = str(spec[0])
                    state = str(spec[1]).upper()
                    lines.append(f'        {{')
                    lines.append(f'            var _mc = {seed_factory}.Services.GetRequiredKeyedService<Polly.CircuitBreaker.CircuitBreakerManualControl>("{policy_name}");')
                    if state == 'OPEN':
                        lines.append(f'            await _mc.IsolateAsync();')
                    else:
                        lines.append(f'            await _mc.CloseAsync();')
                    lines.append(f'        }}')
        lines.append("        try")
        lines.append("        {")
        lines.extend(_compile_test_run_csharp(run_ops, captures, indent=3))
        lines.append("        }")
        lines.append("        finally")
        lines.append("        {")
        if primary_schema:
            lines.append("            ResetStore();")
        lines.append("        }")
        lines.append("    }")
        lines.append("")
    lines.append("}")

    with open(os.path.join(tests_dir, "ApiTests.cs"), 'w') as f:
        f.write('\n'.join(lines))

    print(f"  ✓ C# xUnit tests generated in {tests_dir}/")


def generate_go_pg_tests(bc, output_dir: str, pg_stores: dict):
    """Emit a Go test file that boots a PostgreSQL container via testcontainers-go
    before running the test binary.

    - `TestMain` starts `postgres:16-alpine`, runs the schema DDL, sets the
      service's `DB_CONNECTION_STRING` env var so buildHandler() points at the
      container, then calls `m.Run()`.
    - `_pgExec(t, sql)` and `_pgTruncate(t)` are helpers used by (setup …) and
      (clean …) ops to TRUNCATE / run SQL_SEED between tests.
    - Each test then uses the existing `newTestServer(t)` helper — the same one
      in-memory tests use — so run ops don't need a different emitter.
    """
    primary_store_name, primary_store_def = next(iter(pg_stores.items()))
    primary_schema = primary_store_def['schema']
    primary_table = primary_store_def.get('table') or primary_schema.lower() + 's'
    ddl = generate_postgres_ddl(primary_store_def)
    # Escape backticks so the raw-string DDL constant stays a single Go literal.
    ddl_go = ddl.replace('`', '` + "`" + `')

    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append(f"// AILang Transpiler Output — Go Testcontainers PG tests for {bc.service['name']}")
    lines.append("// Auto-generated from (tests ...) section — DO NOT EDIT")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("package main")
    lines.append("")
    lines.append("import (")
    lines.append('\t"context"')
    lines.append('\t"encoding/json"')
    lines.append('\t"fmt"')
    lines.append('\t"io"')
    lines.append('\t"log"')
    lines.append('\t"net/http"')
    lines.append('\t"net/http/httptest"')
    lines.append('\t"os"')
    lines.append('\t"strings"')
    lines.append('\t"testing"')
    lines.append("")
    lines.append('\t"github.com/jackc/pgx/v5"')
    lines.append('\t"github.com/testcontainers/testcontainers-go"')
    lines.append('\t"github.com/testcontainers/testcontainers-go/modules/postgres"')
    lines.append('\t"github.com/testcontainers/testcontainers-go/wait"')
    lines.append(")")
    lines.append("")
    lines.append("var _ = strings.HasPrefix")
    lines.append("var _ = fmt.Sprintf")
    lines.append("var _ io.Reader = nil")
    lines.append("var _ = json.Unmarshal")
    lines.append("")
    lines.append("var _pgConnStr string  // set by TestMain before m.Run()")
    lines.append("")
    lines.append("// DDL runs once at container start. The service's buildHandler() connects")
    lines.append("// to the same container via DB_CONNECTION_STRING set below.")
    lines.append(f"const _ddl = `{ddl_go}`")
    lines.append("")
    lines.append("func TestMain(m *testing.M) {")
    lines.append("\tctx := context.Background()")
    lines.append("\tcontainer, err := postgres.Run(ctx, \"postgres:16-alpine\",")
    lines.append('\t\tpostgres.WithDatabase("ailang_test"),')
    lines.append('\t\tpostgres.WithUsername("postgres"),')
    lines.append('\t\tpostgres.WithPassword("postgres"),')
    lines.append('\t\ttestcontainers.WithWaitStrategy(wait.ForLog("database system is ready to accept connections").WithOccurrence(2)),')
    lines.append("\t)")
    lines.append('\tif err != nil { log.Fatalf("postgres container start: %v", err) }')
    lines.append("\tdefer func() {")
    lines.append("\t\tif err := testcontainers.TerminateContainer(container); err != nil {")
    lines.append('\t\t\tlog.Printf("postgres container terminate: %v", err)')
    lines.append("\t\t}")
    lines.append("\t}()")
    lines.append('\t_pgConnStr, err = container.ConnectionString(ctx, "sslmode=disable")')
    lines.append('\tif err != nil { log.Fatalf("postgres connection string: %v", err) }')
    lines.append('\tos.Setenv("DB_CONNECTION_STRING", _pgConnStr)')
    lines.append("")
    lines.append("\tconn, err := pgx.Connect(ctx, _pgConnStr)")
    lines.append('\tif err != nil { log.Fatalf("pgx connect for DDL: %v", err) }')
    lines.append("\tif _, err := conn.Exec(ctx, _ddl); err != nil {")
    lines.append('\t\tlog.Fatalf("pgx DDL exec: %v", err)')
    lines.append("\t}")
    lines.append("\tconn.Close(ctx)")
    lines.append("")
    lines.append("\tos.Exit(m.Run())")
    lines.append("}")
    lines.append("")
    lines.append("// newTestServer wraps buildHandler() in httptest — buildHandler() connects")
    lines.append("// to the testcontainer via DB_CONNECTION_STRING set in TestMain.")
    lines.append("func newTestServer(t *testing.T) *httptest.Server {")
    lines.append("\tt.Helper()")
    lines.append("\tts := httptest.NewServer(buildHandler())")
    lines.append("\tt.Cleanup(ts.Close)")
    lines.append("\treturn ts")
    lines.append("}")
    lines.append("")
    lines.append("// _pgExec runs a single raw SQL statement against the testcontainer.")
    lines.append("// Used by (setup (SQL_SEED …)) and by RESET_STORE's TRUNCATE.")
    lines.append("func _pgExec(t *testing.T, sql string) {")
    lines.append("\tt.Helper()")
    lines.append("\tctx := context.Background()")
    lines.append("\tconn, err := pgx.Connect(ctx, _pgConnStr)")
    lines.append('\tif err != nil { t.Fatalf("pgx connect: %v", err) }')
    lines.append("\tdefer conn.Close(ctx)")
    lines.append("\tif _, err := conn.Exec(ctx, sql); err != nil {")
    lines.append('\t\tt.Fatalf("pgx exec %q: %v", sql, err)')
    lines.append("\t}")
    lines.append("}")
    lines.append("")

    for test in bc.tests:
        ident = 'Test' + pascal_case(_test_name_to_ident(test['name']))
        run_ops = test.get('run') or []
        setup_ops = test.get('setup_ops') or []
        clean_ops = test.get('clean') or []
        captures: set = set()
        lines.append(f"func {ident}(t *testing.T) {{")
        # Setup (SQL_SEED, RESET_STORE) — pg variants run raw SQL.
        for op in setup_ops:
            if not (isinstance(op, list) and op):
                continue
            head = str(op[0]).upper()
            if head == 'RESET_STORE':
                lines.append(f'\t_pgExec(t, `TRUNCATE TABLE "{primary_table}" CASCADE`)')
            elif head == 'SQL_SEED':
                raw_sql = str(op[2]) if len(op) > 2 else ''
                # Use backtick raw literal to avoid escaping the inline INSERT.
                safe_sql = raw_sql.replace('`', '` + "`" + `')
                lines.append(f'\t_pgExec(t, `{safe_sql}`)')
        lines.append("\tts := newTestServer(t)")
        lines.append("\t_ = ts")
        lines.extend(_compile_test_run_go(run_ops, captures, indent=1))
        # Clean ops — most tests duplicate RESET_STORE in clean; mirror that
        # with a deferred exec so a failing REQUEST doesn't skip the cleanup.
        cleanup_sql = []
        for op in clean_ops:
            if isinstance(op, str):
                if op.strip().upper().startswith('RESET_STORE'):
                    cleanup_sql.append(f'TRUNCATE TABLE "{primary_table}" CASCADE')
                continue
            if not (isinstance(op, list) and op):
                continue
            head = str(op[0]).upper()
            if head == 'RESET_STORE':
                cleanup_sql.append(f'TRUNCATE TABLE "{primary_table}" CASCADE')
            elif head == 'SQL_SEED':
                raw_sql = str(op[2]) if len(op) > 2 else ''
                cleanup_sql.append(raw_sql)
        for sql in cleanup_sql:
            safe_sql = sql.replace('`', '` + "`" + `')
            lines.append(f'\t_pgExec(t, `{safe_sql}`)')
        lines.append("}")
        lines.append("")

    with open(os.path.join(output_dir, "main_test.go"), 'w') as f:
        f.write('\n'.join(lines))

    print(f"  ✓ Go Testcontainers (PG) tests generated in {output_dir}/main_test.go")


def generate_go_tests(bc, output_dir: str):
    """Emit main_test.go using httptest.NewServer(buildHandler())."""
    if not bc.tests:
        return
    pg_stores = {n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'}
    if pg_stores:
        generate_go_pg_tests(bc, output_dir, pg_stores)
        return

    # Pre-scan: do any tests actually use delay_ms / override_time / force_circuit_state / seed_data?
    # Used to gate helpers and imports so services that don't need them don't grow
    # unused-symbol errors.
    any_delay_ms = False
    any_override_time = False
    any_force_cb = False
    any_seed_data = False
    for test in bc.tests:
        for ep_list in (test.get('mock_services') or {}).values():
            for ep in ep_list:
                if (ep.get('delay_ms') or 0) > 0:
                    any_delay_ms = True
        for op in (test.get('setup_ops') or []):
            if isinstance(op, list) and op:
                if op[0] == 'override_time': any_override_time = True
                elif op[0] == 'force_circuit_state': any_force_cb = True
                elif op[0] == 'seed_data': any_seed_data = True

    lines = []
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append(f"// AILang Transpiler Output — Go httptest for {bc.service['name']}")
    lines.append("// Auto-generated from (tests ...) section — DO NOT EDIT")
    lines.append("// ═══════════════════════════════════════════════════════════════")
    lines.append("package main")
    lines.append("")
    lines.append("import (")
    lines.append('\t"encoding/json"')
    lines.append('\t"fmt"')
    lines.append('\t"io"')
    lines.append('\t"net/http"')
    lines.append('\t"net/http/httptest"')
    lines.append('\t"strings"')
    lines.append('\t"testing"')
    lines.append('\t"time"')
    lines.append(")")
    lines.append("")
    lines.append("// newTestServer spins a fresh service graph per test — full isolation.")
    lines.append("// Closes automatically when the test finishes.")
    lines.append("func newTestServer(t *testing.T) *httptest.Server {")
    lines.append("\tt.Helper()")
    lines.append("\tts := httptest.NewServer(buildHandler())")
    lines.append("\tt.Cleanup(ts.Close)")
    lines.append("\treturn ts")
    lines.append("}")
    lines.append("")
    # Test helpers — only emitted when at least one test uses them, so services
    # with trivial test suites don't drag in unused symbols.
    if any_seed_data or any_override_time:
        lines.append("// _mustParseTime — parse ISO-8601 timestamps in seed_data / override_time blocks.")
        lines.append("// Any parse error panics so the test fails loudly rather than silently zeroing.")
        lines.append("func _mustParseTime(s string) time.Time {")
        lines.append("\tt, err := time.Parse(time.RFC3339, s)")
        lines.append('\tif err != nil { panic("invalid timestamp literal: " + s + " — " + err.Error()) }')
        lines.append("\treturn t")
        lines.append("}")
        lines.append("")
    if any_override_time:
        lines.append("func _ptrWeekday(w time.Weekday) *time.Weekday { return &w }")
        lines.append("")
    # silence-unused to keep imports in single-test edge cases
    lines.append("var _ = strings.HasPrefix")
    lines.append("var _ = fmt.Sprintf")
    lines.append("var _ io.Reader = nil")
    lines.append("var _ = json.Unmarshal")
    lines.append("var _ = time.Second")
    lines.append("")

    for test in bc.tests:
        ident = 'Test' + pascal_case(_test_name_to_ident(test['name']))
        run_ops = test.get('run') or []
        mocks = test.get('mock_services') or {}
        setup_ops = test.get('setup_ops') or []
        captures: set = set()
        lines.append(f"func {ident}(t *testing.T) {{")
        # Mocks must be started and env vars set BEFORE newTestServer — buildHandler
        # reads serviceURLs at wire-up time.
        for svc, endpoints in mocks.items():
            env_var = _env_var_for_dependency(bc, svc)
            mock_var = f"_mock_{svc}"
            lines.append(f"\t{mock_var} := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {{")
            lines.append('\t\tw.Header().Set("Content-Type", "application/json")')
            for ep in endpoints:
                json_body = _mock_body_to_json(ep.get('body'))
                delay = ep.get('delay_ms') or 0
                lines.append(f'\t\tif r.Method == "{ep["method"]}" && r.URL.Path == "{ep["path"]}" {{')
                if delay > 0:
                    # Sleep inside the mock goroutine; the client's per-attempt
                    # ctx.WithTimeout cancels the request first, surfacing as
                    # context.DeadlineExceeded in callService.
                    lines.append(f'\t\t\ttime.Sleep({delay} * time.Millisecond)')
                lines.append(f'\t\t\tw.WriteHeader({ep["status"]})')
                lines.append(f'\t\t\tw.Write([]byte(`{json_body}`))')
                lines.append('\t\t\treturn')
                lines.append('\t\t}')
            lines.append("\t\thttp.NotFound(w, r)")
            lines.append("\t}))")
            lines.append(f"\tdefer {mock_var}.Close()")
            lines.append(f'\tt.Setenv("{env_var}", {mock_var}.URL)')
        lines.append("\tts := newTestServer(t)")
        lines.append("\t_ = ts")
        # ── setup_ops — wire AFTER buildHandler since buildHandler() resets
        # the test-seam state on every call. ──
        for setup_op in setup_ops:
            if not (isinstance(setup_op, list) and setup_op):
                continue
            op_name = setup_op[0]
            if op_name == 'seed_data':
                lines.extend(_emit_seed_data_go(setup_op, bc.stores, bc.schemas, pad='\t'))
            elif op_name == 'override_time':
                # `(override_time (now ISO) (day_of_week tuesday))` — wire a FakeClock
                # from the declared overrides. Unset fields fall through to SystemClock.
                fake_parts = []
                for spec in setup_op[1:]:
                    if not (isinstance(spec, list) and len(spec) >= 2):
                        continue
                    key = str(spec[0])
                    val = spec[1]
                    if key == 'day_of_week':
                        wk_map = {
                            'sunday': 'time.Sunday', 'monday': 'time.Monday',
                            'tuesday': 'time.Tuesday', 'wednesday': 'time.Wednesday',
                            'thursday': 'time.Thursday', 'friday': 'time.Friday',
                            'saturday': 'time.Saturday',
                        }
                        go_wk = wk_map.get(str(val).lower(), 'time.Monday')
                        fake_parts.append(f'FixedDayOfWeek: _ptrWeekday({go_wk})')
                    elif key == 'now':
                        iso = str(val).strip('"')
                        fake_parts.append(f'FixedNow: _mustParseTime("{iso}")')
                init = ", ".join(fake_parts)
                lines.append(f"\t_clock = FakeClock{{{init}}}")
            elif op_name == 'force_circuit_state':
                # `(force_circuit_state (<policy_name> OPEN))` — short-circuits
                # callService via the package-level _forcedCircuitStates map.
                for spec in setup_op[1:]:
                    if not (isinstance(spec, list) and len(spec) >= 2):
                        continue
                    policy_name = str(spec[0])
                    state = str(spec[1]).upper()
                    lines.append(f'\t_forcedCircuitStates["{policy_name}"] = "{state}"')
        lines.extend(_compile_test_run_go(run_ops, captures, indent=1))
        lines.append("}")
        lines.append("")

    with open(os.path.join(output_dir, "main_test.go"), 'w') as f:
        f.write('\n'.join(lines))

    print(f"  ✓ Go httptest generated in {output_dir}/main_test.go")


# ============================================================================
# MAIN
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print("AILang Transpiler v0.2")
        print("Usage: python transpiler.py <bytecode.json> [--target csharp|go|all] [--aot]")
        print()
        print("  --aot    Emit C# project with <PublishAot>true</PublishAot> and a")
        print("           source-generated JSON context. Implies single-binary publish.")
        print("           Rejected with --target go (Go does not support NativeAOT in this transpiler).")
        sys.exit(1)

    filepath = sys.argv[1]
    target = "all"
    aot = "--aot" in sys.argv

    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]

    if aot and target in ("go", "all"):
        print(f"ERROR: --aot is only supported with --target csharp (got --target {target}).", file=sys.stderr)
        print("       NativeAOT is a .NET concept; the Go codegen does not produce AOT output.", file=sys.stderr)
        sys.exit(2)

    print(f"AILang Transpiler v0.2")
    print(f"  Source: {filepath}")
    print(f"  Target: {target}")
    print()

    # Detect format and use appropriate parser
    if filepath.endswith('.sexpr'):
        print(f"  Format: S-expression")
        # Import here to avoid issues when only using JSON
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sexpr_bytecode_parser import SExprBytecodeParser
        bc = SExprBytecodeParser(filepath)
    else:
        print(f"  Format: JSON")
        bc = BytecodeParser(filepath)
    service_name = bc.service['name']

    print(f"  Parsed service: {service_name}")
    print(f"  Schemas: {list(bc.schemas.keys())}")
    print(f"  Stores: {list(bc.stores.keys())}")
    print(f"  Routes: {len(bc.routes)}")
    print(f"  Handlers: {len(bc.handlers)}")
    print(f"  Tests: {len(bc.tests)}")
    print()

    base_dir = os.path.dirname(filepath) or '.'
    # Use the source filename stem for the output dir — multiple .sexpr files
    # can declare the same (service X) name (e.g. product_service.sexpr vs
    # product_service_pg.sexpr both with service=product_service). Keying off
    # the stem keeps their generated dirs distinct without renaming services.
    source_stem = os.path.splitext(os.path.basename(filepath))[0]

    if target in ('csharp', 'all'):
        output_dir = os.path.join(base_dir, f"generated-csharp-{source_stem}")
        generate_csharp(bc, output_dir, aot=aot)
        generate_csharp_tests(bc, output_dir)

    if target in ('go', 'all'):
        output_dir = os.path.join(base_dir, f"generated-go-{source_stem}")
        generate_go(bc, output_dir)
        generate_go_tests(bc, output_dir)

    print()
    print("  Done!")


if __name__ == "__main__":
    main()
