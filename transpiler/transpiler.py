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
        if ailang_type == 'list' and list_of:
            inner = TypeMapper.CSHARP_TYPES.get(list_of, list_of)
            return f"List<{inner}>"
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


def path_to_go_sprintf(path: str):
    """Convert '/products/{item.product_id}' to (fmt_string_with_%v, [arg_exprs]).

    Each {x.y.z} placeholder becomes %v; the corresponding arg is `x.Y.Z` (Go
    idioms: first segment lowercase, subsequent PascalCased)."""
    import re
    args = []
    def repl(m):
        expr = m.group(1)
        if '.' in expr:
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
        key = inst['key']
        if is_pg:
            lines.append(f"{pad}var {inst['assign']} = await {store_var}.FindAsync({key});")
        else:
            lines.append(f"{pad}var {inst['assign']} = {store_var}.Find({key});")

    elif op == 'STORE_FIND_BY':
        lines.extend(_compile_store_find_by_csharp(inst, indent, ctx))

    elif op == 'STORE_FILTER':
        lines.extend(_compile_store_filter_csharp(inst, indent, ctx))

    elif op == 'STORE_INSERT':
        if is_pg:
            lines.append(f"{pad}var {inst['assign']} = await {store_var}.InsertAsync({inst['data']});")
        else:
            lines.append(f"{pad}var {inst['assign']} = {store_var}.Insert({inst['data']});")

    elif op == 'STORE_UPDATE':
        if is_pg:
            lines.append(f"{pad}var {inst['assign']} = await {store_var}.UpdateAsync({inst['key']}, {inst['data']});")
        else:
            lines.append(f"{pad}var {inst['assign']} = {store_var}.Update({inst['key']}, {inst['data']});")

    elif op == 'STORE_UPDATE_FIELDS':
        fields = inst.get('fields', {})
        for field, value in fields.items():
            lines.append(f'{pad}// STORE_UPDATE_FIELDS: {field} = {value}')

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
        lines.append(f"{pad}var {inst['assign']} = await ctx.Request.ReadFromJsonAsync<{inst['schema']}Dto>();")
        lines.append(f"{pad}if ({inst['assign']} is null)")
        lines.append(f"{pad}    return Results.BadRequest(AiLangError.InvalidBody(\"{inst['schema']}\", \"body is null\"));")

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
        lines.extend(compile_error_csharp(inst, pad))

    elif op == 'ERR_CUSTOM':
        err_type = inst['type']
        params = inst.get('params', {}) or {}
        custom_errors = ctx.get('custom_errors', {}) or {}
        entry = custom_errors.get(err_type)
        if entry:
            factory = err_name_to_factory(err_type)
            declared = entry.get('params', []) or []
            status = entry.get('http_status', 422)
            # Pass params in the declared order so the factory signature matches.
            args = [_cs_value(params.get(p, "")) for p in declared]
            lines.append(f'{pad}return Results.Json(AiLangError.{factory}({", ".join(args)}), statusCode: {status});')
        else:
            tag = err_type_tag(err_type)
            lines.append(f'{pad}return Results.Json(AiLangError.BusinessRule("{tag}", ""), statusCode: 422);')

    elif op == 'CALL_SERVICE':
        # (CALL_SERVICE name (method X) (path "..") (idempotent b) (body {...}) → assign)
        service = inst['service']
        method = inst.get('method', 'GET').upper()
        path_expr = path_to_csharp_interpolated(inst.get('path', ''))
        assign = inst['assign']
        body = inst.get('body')
        body_expr = 'null'
        if body:
            pairs = ", ".join(f'["{k}"] = {_cs_value(v)}' for k, v in body.items())
            body_expr = f'new System.Collections.Generic.Dictionary<string, object?> {{ {pairs} }}'
        lines.append(
            f'{pad}System.Text.Json.Nodes.JsonNode? {assign} = await CallServiceAsync(httpClientFactory, "{service}", "{method}", {path_expr}, {body_expr});'
        )
        ctx.setdefault('locals', {})[assign] = 'JsonNode'

    elif op == 'MATCH':
        lines.extend(compile_match_csharp(inst, indent, ctx))

    elif op == 'BRANCH':
        lines.extend(compile_branch_csharp(inst, indent, ctx))

    elif op == 'EMIT_EVENT':
        lines.append(f'{pad}Console.WriteLine($"[EVENT] {inst["event"]} | data={{{inst.get("data", "")}}}" );')

    elif op == 'DEFINE_VAR':
        lines.extend(_compile_define_var_csharp(inst, pad, ctx))

    elif op == 'SET_VAR':
        lines.append(f"{pad}{inst['name']} = {_cs_value(inst.get('value'))};")

    elif op == 'COMPUTE':
        left = _cs_value(inst.get('left'))
        right = _cs_value(inst.get('right'))
        operator = inst.get('operator', '+')
        assign = inst['assign']
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
            lines.append(f"{pad}{target}.Add({_cs_value(inst['value'])});")
        else:
            # Object-construction append: look up target's declared element type.
            locals_dict = ctx.get('locals', {}) or {}
            ttype = locals_dict.get(target, 'List<object>')
            elem = extract_list_elem(ailang_type_to_csharp(ttype))
            fields = inst.get('object_fields', {})
            init = ", ".join(f"{pascal_case(k)} = {_cs_value(v)}" for k, v in fields.items())
            lines.append(f"{pad}{target}.Add(new {elem} {{ {init} }});")

    elif op == 'ITERATE':
        collection = inst['collection']
        item = inst['item']
        lines.append(f"{pad}foreach (var {item} in {collection})")
        lines.append(f"{pad}{{")
        # Inner scope: copy locals and register `item` (unknown element type).
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        inner_ctx['locals'][item] = 'object'
        for sub in inst.get('body', []):
            lines.extend(compile_instruction_csharp(sub, indent + 1, inner_ctx))
        lines.append(f"{pad}}}")

    elif op == 'CONSTRUCT':
        schema = inst['schema']
        fields = inst.get('fields', {})
        parts = []
        for k, v in fields.items():
            parts.append(f"{pascal_case(k)} = {_cs_value(v)}")
        body = ", ".join(parts)
        lines.append(f"{pad}var {inst['assign']} = new {schema} {{ {body} }};")
        ctx.setdefault('locals', {})[inst['assign']] = schema

    elif op == 'GET_CURRENT':
        from_ = inst.get('from', 'auth')
        field = inst.get('field', 'user')
        assign = inst['assign']
        if from_ == 'auth' and field == 'user':
            lines.append(f'{pad}var {assign} = ctx.User.FindFirst("sub")?.Value ?? "";')
        elif from_ == 'clock' and field == 'now':
            lines.append(f"{pad}var {assign} = DateTime.UtcNow;")
        elif from_ == 'clock' and field in ('today', 'day_of_week'):
            call = 'DateTime.UtcNow.Date' if field == 'today' else 'DateTime.UtcNow.DayOfWeek'
            lines.append(f"{pad}var {assign} = {call};")
        else:
            lines.append(f"{pad}var {assign} = default!;  // GET_CURRENT {from_}.{field} (unsupported)")

    elif op == 'LOG':
        level = inst.get('level', 'INFO')
        lines.append(f'{pad}Console.WriteLine($"[{level}] {inst["message"]}");')

    return [l for l in lines if l != ""]


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
        return f'/* TODO nested: {v} */ default!'
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
    if value is None:
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
    if vtype == 'string':
        return [f"{pad}{cs_type} {name} = {_cs_value(value)};"]
    if vtype == 'object' and s.startswith('{'):
        return [f"{pad}{cs_type} {name} = default!;  // TODO: object initializer {s}"]
    return [f"{pad}{cs_type} {name} = {s};"]


def _compile_compute_fn_csharp(inst: dict, pad: str, ctx: dict) -> list[str]:
    fn = inst['function']
    args = [_cs_value(a) for a in inst.get('args', [])]
    assign = inst['assign']
    fn_map = {
        'round': 'Math.Round', 'floor': 'Math.Floor', 'ceil': 'Math.Ceiling',
        'min': 'Math.Min', 'max': 'Math.Max', 'abs': 'Math.Abs',
    }
    if fn == 'length':
        src = args[0] if args else 'default'
        return [f"{pad}var {assign} = {src}.Count;"]
    if fn in fn_map:
        return [f"{pad}var {assign} = {fn_map[fn]}({', '.join(args)});"]
    return [f"{pad}var {assign} = default!;  // COMPUTE_FN: unsupported function {fn}"]


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


def compile_error_csharp(inst: dict, pad: str) -> list[str]:
    """Compile error instructions to C#."""
    op = inst['op']
    lines = []

    if op == 'ERR_NOT_FOUND':
        id_expr = _cs_value(inst["id"])
        # Convert.ToString handles both value types (Guid, int) and reference
        # types — and safely returns "" for null refs — so the member access
        # doesn't need a `?.` that would reject non-nullable value types.
        lines.append(f'{pad}return Results.NotFound(AiLangError.NotFound("{inst["entity"]}", Convert.ToString({id_expr}) ?? ""));')
    elif op == 'ERR_VALIDATION':
        lines.append(f'{pad}return Results.BadRequest(AiLangError.Validation("{inst["field"]}", "{inst["reason"]}"));')
    elif op == 'ERR_BUSINESS_RULE':
        lines.append(f'{pad}return Results.Json(AiLangError.BusinessRule("{inst["rule"]}", "{inst["detail"]}"), statusCode: 422);')
    elif op == 'ERR_INVALID_BODY':
        lines.append(f'{pad}return Results.BadRequest(AiLangError.InvalidBody("{inst["schema"]}", "invalid"));')
    elif op == 'ERR_UNAUTHORIZED':
        lines.append(f'{pad}return Results.Json(AiLangError.Unauthorized(), statusCode: 401);')
    elif op == 'ERR_UNAVAILABLE':
        lines.append(f'{pad}return Results.Json(AiLangError.Unavailable("{inst.get("service", "")}"), statusCode: 503);')

    return lines


def compile_match_csharp(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    """Compile MATCH instruction to C# if/else."""
    pad = "    " * indent
    lines = []
    value = inst['value']
    cases = inst.get('cases', {})

    if 'OK' in cases and ('NONE' in cases or 'ERR' in cases):
        # RESULT match (find/read_body pattern) — OK/NONE or OK/ERR
        fail_case = 'NONE' if 'NONE' in cases else 'ERR'
        lines.append(f"{pad}if ({value} is null)")
        lines.append(f"{pad}{{")
        for sub in cases[fail_case]:
            lines.extend(compile_instruction_csharp(sub, indent + 1, ctx))
        lines.append(f"{pad}}}")

        if cases.get('OK'):
            # If the fail branch always returns, emit OK inline at the same indent.
            has_return_in_fail = any('return' in line for line in lines)
            if not has_return_in_fail:
                lines.append(f"{pad}else")
                lines.append(f"{pad}{{")
            for sub in cases['OK']:
                lines.extend(compile_instruction_csharp(sub, indent if has_return_in_fail else indent + 1, ctx))
            if not has_return_in_fail:
                lines.append(f"{pad}}}")

    return lines


def compile_branch_csharp(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    """Compile BRANCH instruction to C# if/else."""
    pad = "    " * indent
    lines = []
    condition = inst.get('condition', '')
    cases = inst.get('cases', {})

    if isinstance(condition, dict):
        # Both operands go through _cs_value: member access gets PascalCased, string
        # literals (StringLiteral) get quoted, identifiers pass through.
        left = _cs_value(condition.get('left', ''))
        op = condition.get('op', '==')
        right = _cs_value(condition.get('right', ''))
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


def generate_csharp(bc, output_dir: str):
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
    service_refs = {n: d for n, d in (bc.dependencies or {}).items() if d.get('type') == 'SERVICE_REF'}
    for svc_name, svc_def in service_refs.items():
        env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
        default_url = svc_def.get('default', 'http://localhost')
        lines.append(f"// ── DI: HttpClient for service_ref {svc_name} ──")
        lines.append(f'builder.Services.AddHttpClient("{svc_name}", c =>')
        lines.append("{")
        lines.append(f'    var baseUrl = Environment.GetEnvironmentVariable("{env_var}") ?? "{default_url}";')
        lines.append("    c.BaseAddress = new Uri(baseUrl);")
        lines.append("});")
    if service_refs:
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

    lines.append(f'var api = app.MapGroup("{base_path}");')
    lines.append("")

    # Primary store (first store is the dominant one for handler codegen)
    primary_store_name, primary_store_def = next(iter(bc.stores.items())) if bc.stores else (None, None)
    primary_schema = primary_store_def['schema'] if primary_store_def else None

    for route in bc.routes:
        handler_name = route['handler']
        handler_def = bc.handlers[handler_name]
        method = route['method']
        path = route['path'].replace(base_path, '') or '/'
        auth = handler_def.get('auth')
        instructions = handler_def.get('instructions', [])

        def _walk_ops(inst_list):
            for i in inst_list:
                yield i
                cases = i.get('cases') or {}
                for case_insts in cases.values():
                    if isinstance(case_insts, list):
                        yield from _walk_ops(case_insts)

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

        param_str = ", ".join(lambda_params)
        is_async = is_pg or needs_body or uses_call_service or any(
            op in ('SQL_QUERY', 'SQL_STREAM', 'SQL_EXEC', 'STORE_FIND_BY', 'STORE_FILTER', 'STREAM_RESPOND')
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
            lines.append("        return Results.Json(AiLangError.Unauthorized(), statusCode: 401);")
            lines.append("")

        ctx = {
            'is_pg': is_pg,
            'store_var': camel_case(primary_store_name) if primary_store_name else 'store',
            'store_def': primary_store_def,
            'returns': handler_def.get('returns'),
            'custom_errors': bc.custom_errors,
        }
        for inst in instructions:
            lines.extend(compile_instruction_csharp(inst, 1, ctx))

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
    lines.append('    return Results.Ok(new { token = new System.IdentityModel.Tokens.Jwt.JwtSecurityTokenHandler().WriteToken(token) });')
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

    lines.append(f'app.Run("http://0.0.0.0:{port}");')
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
            lines.extend(generate_store_csharp(schema, schema_def))
        lines.append("")

    # Error model
    lines.extend(generate_error_model_csharp(bc.custom_errors))

    with open(os.path.join(output_dir, "Program.cs"), 'w') as f:
        f.write('\n'.join(lines))

    # .csproj — include Npgsql when a PG store is present
    pkg_refs = [
        '    <PackageReference Include="Microsoft.AspNetCore.Authentication.JwtBearer" Version="10.0.0" />',
        '    <PackageReference Include="System.IdentityModel.Tokens.Jwt" Version="8.2.1" />',
    ]
    if is_pg:
        pkg_refs.append('    <PackageReference Include="Npgsql" Version="10.0.0" />')

    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        '  </PropertyGroup>\n'
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
        lines.append("}")

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
        "    using var resp = await client.SendAsync(req);",
        "    if (!resp.IsSuccessStatusCode) return null;",
        "    var json = await resp.Content.ReadAsStringAsync();",
        "    if (string.IsNullOrWhiteSpace(json)) return null;",
        "    return System.Text.Json.Nodes.JsonNode.Parse(json);",
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

    # Partition fields: auto-gen (db-managed) vs writable (come from DTO)
    auto_fields = [
        (f, m) for f, m in mapping
        if schema_def.get('fields', {}).get(f, {}).get('auto_gen', False)
    ]
    writable_fields = [
        (f, m) for f, m in mapping
        if not schema_def.get('fields', {}).get(f, {}).get('auto_gen', False)
    ]

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
            # Use column name = field_name by convention. Reader column name lookup.
            lines.append(f'        {prop} = reader.IsDBNull(reader.GetOrdinal("{field_name}")) ? default : ({cs_type})reader.GetValue(reader.GetOrdinal("{field_name}")),')
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


def generate_store_csharp(schema: str, schema_def: dict) -> list[str]:
    """Generate C# in-memory store from AILang store definition."""
    fields = schema_def.get('fields', {})
    dto_fields = {k: v for k, v in fields.items() if not v.get('auto_gen')}
    auto_gen_fields = {k: v for k, v in fields.items() if v.get('auto_gen')}

    lines = []
    lines.append(f"// STORE: InMemory{schema}Store (from bytecode)")
    lines.append(f"public interface I{schema}Store")
    lines.append("{")
    lines.append(f"    List<{schema}> ListAll();")
    lines.append(f"    {schema}? Find(Guid id);")
    lines.append(f"    {schema} Insert({schema}Dto data);")
    lines.append(f"    {schema}? Update(Guid id, {schema}Dto data);")
    lines.append("    bool Delete(Guid id);")
    lines.append("}")
    lines.append("")

    # Primary constructor keeps the file-state minimal.
    lines.append(f"public class InMemory{schema}Store : I{schema}Store")
    lines.append("{")
    lines.append(f"    private readonly ConcurrentDictionary<Guid, {schema}> _data = new();")
    lines.append("")
    lines.append(f"    public List<{schema}> ListAll() => [.. _data.Values];")
    lines.append(f"    public {schema}? Find(Guid id) => _data.GetValueOrDefault(id);")
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
    # All auto_gen uuid fields default to NewGuid → Id is known; indexer is safe
    lines.append("        _data[item.Id] = item;")
    lines.append("        return item;")
    lines.append("    }")
    lines.append("")

    # Update: preserve original Id + created_at, regenerate updated_at
    lines.append(f"    public {schema}? Update(Guid id, {schema}Dto data)")
    lines.append("    {")
    lines.append("        if (!_data.TryGetValue(id, out var existing)) return null;")
    lines.append(f"        {schema} item = new()")
    lines.append("        {")
    lines.append("            Id = id,")
    for field_name, field_def in auto_gen_fields.items():
        if field_name == 'id':
            continue
        prop = pascal_case(field_name)
        if field_name == 'updated_at':
            lines.append(f"            {prop} = DateTime.UtcNow,")
        else:
            lines.append(f"            {prop} = existing.{prop},")
    for field_name in dto_fields:
        prop = pascal_case(field_name)
        lines.append(f"            {prop} = data.{prop}!,")
    lines.append("        };")
    lines.append("        _data[id] = item;")
    lines.append("        return item;")
    lines.append("    }")
    lines.append("")
    lines.append("    public bool Delete(Guid id) => _data.TryRemove(id, out _);")
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

    # Pre-scan handlers to decide which optional imports we need.
    def _walk_ops(h):
        for inst in h.get('instructions', []):
            yield inst.get('op')
            for c in (inst.get('cases') or {}).values():
                if isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict):
                            yield sub.get('op')
            for sub in inst.get('body', []) or []:
                if isinstance(sub, dict):
                    yield sub.get('op')
    all_ops = set()
    for h in bc.handlers.values():
        for op in _walk_ops(h):
            all_ops.add(op)
            # Also walk nested COMPUTE_FN args for math detection downstream
    has_in_memory_store = any(s.get('provider') != 'PostgreSQL' for s in bc.stores.values())
    needs_math = 'COMPUTE_FN' in all_ops
    needs_http_client = 'CALL_SERVICE' in all_ops

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
    if is_pg or needs_http_client:
        lines.append('\t"context"')
    lines.append('\t"encoding/json"')
    lines.append('\t"fmt"')
    if is_pg:
        lines.append('\t"errors"')
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
    if has_in_memory_store:
        lines.append('\t"sync"')
    lines.append('\t"time"')
    lines.append("")
    lines.append('\t"github.com/google/uuid"')
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

    # Stores
    for store_name, store_def in bc.stores.items():
        schema = store_def['schema']
        schema_def = bc.schemas[schema]
        if store_def.get('provider') == 'PostgreSQL':
            lines.extend(generate_postgres_store_go(store_name, store_def, schema_def))
        else:
            lines.extend(generate_store_go(schema, schema_def))
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
    lines.append("}")
    lines.append("")

    if uses_call_service_anywhere:
        lines.extend(generate_service_client_go())
        lines.append("")

    # Handlers (generated before the mux so we can reference them)
    for handler_name, handler_def in bc.handlers.items():
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

    # Main
    lines.append("func main() {")
    lines.append(f'\t// JWT env var: {jwt_env}')
    if is_pg:
        lines.append(f'\tconnStr := os.Getenv("{db_conn_env}")')
        lines.append('\tif connStr == "" {')
        lines.append('\t\tconnStr = "postgres://postgres:postgres@localhost:5432/ailang"')
        lines.append("\t}")
        lines.append("\tctx := context.Background()")
        lines.append("\tpool, err := pgxpool.New(ctx, connStr)")
        lines.append('\tif err != nil { log.Fatalf("pgx pool: %v", err) }')
        lines.append("\tdefer pool.Close()")
        for store_name, store_def in bc.stores.items():
            schema = store_def['schema']
            lines.append(f"\tvar store I{schema}Store = NewPostgres{schema}Store(pool)")
    else:
        for store_name, store_def in bc.stores.items():
            lines.append(f"\tstore := New{store_def['schema']}Store()")
    # Resolve SERVICE_REF base URLs at startup (env var w/ default fallback).
    if uses_call_service_anywhere:
        lines.append("\tserviceURLs := map[string]string{")
        for svc_name, svc_def in service_refs_go.items():
            env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
            default_url = svc_def.get('default', 'http://localhost')
            lines.append(f'\t\t"{svc_name}": func() string {{ if v := os.Getenv("{env_var}"); v != "" {{ return v }}; return "{default_url}" }}(),')
        lines.append("\t}")

    router_fields = []
    if bc.stores:
        router_fields.append("store: store")
    if uses_call_service_anywhere:
        router_fields.append("serviceURLs: serviceURLs")
    lines.append(f"\trouter := &Router{{{', '.join(router_fields)}}}")
    lines.append("\thandler := corsMiddleware(tracingMiddleware(router.mux()))")
    lines.append("")
    lines.append(f'\tfmt.Println("[SERVICE] {service_name} STARTED on :{port}")')
    lines.append(f'\tfmt.Println("[READY]")')
    lines.append(f'\tlog.Fatal(http.ListenAndServe(":{port}", handler))')
    lines.append("}")

    with open(os.path.join(output_dir, "main.go"), 'w') as f:
        f.write('\n'.join(lines))

    # go.mod — bump to 1.22 for the new ServeMux patterns; add pgx when needed
    requires = ['\tgithub.com/google/uuid v1.6.0']
    if is_pg:
        requires.append('\tgithub.com/jackc/pgx/v5 v5.7.5')
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


def generate_service_client_go() -> list[str]:
    """Global helper: callService(ctx, baseURL, method, path, body) → map[string]any.

    Returns the parsed JSON body on 2xx, or nil on transport error / non-2xx so
    the MATCH OK/NONE idiom works with `== nil` (ERR) and `!= nil` (OK).
    """
    return [
        "// ── INTER-SERVICE HTTP HELPER ──",
        "var _httpClient = &http.Client{Timeout: 10 * time.Second}",
        "",
        "func callService(ctx context.Context, baseURL, method, path string, body any) map[string]any {",
        "\tvar reqBody io.Reader",
        "\tif body != nil {",
        "\t\tb, err := json.Marshal(body)",
        "\t\tif err != nil { slog.Error(\"call_service marshal\", \"err\", err); return nil }",
        "\t\treqBody = bytes.NewReader(b)",
        "\t}",
        "\treq, err := http.NewRequestWithContext(ctx, method, baseURL+path, reqBody)",
        "\tif err != nil { slog.Error(\"call_service req\", \"err\", err); return nil }",
        "\tif body != nil { req.Header.Set(\"Content-Type\", \"application/json\") }",
        "\tresp, err := _httpClient.Do(req)",
        "\tif err != nil { slog.Error(\"call_service do\", \"err\", err); return nil }",
        "\tdefer resp.Body.Close()",
        "\tif resp.StatusCode >= 400 {",
        "\t\tslog.Info(\"call_service non-2xx\", \"status\", resp.StatusCode, \"path\", path)",
        "\t\treturn nil",
        "\t}",
        "\tvar out map[string]any",
        "\tif err := json.NewDecoder(resp.Body).Decode(&out); err != nil {",
        "\t\tslog.Error(\"call_service decode\", \"err\", err)",
        "\t\treturn nil",
        "\t}",
        "\treturn out",
        "}",
    ]


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

    auto_fields = [
        (f, m) for f, m in mapping
        if schema_def.get('fields', {}).get(f, {}).get('auto_gen', False)
    ]
    writable_fields = [
        (f, m) for f, m in mapping
        if not schema_def.get('fields', {}).get(f, {}).get('auto_gen', False)
    ]

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


def generate_store_go(schema: str, schema_def: dict) -> list[str]:
    lines = []
    dto_fields = {k: v for k, v in schema_def.get('fields', {}).items() if not v.get('auto_gen')}

    lines.append(f"// STORE: {schema}Store (from bytecode)")
    lines.append(f"type {schema}Store struct {{")
    lines.append(f"\tmu   sync.RWMutex")
    lines.append(f"\tdata map[string]{schema}")
    lines.append("}")
    lines.append("")
    lines.append(f"func New{schema}Store() *{schema}Store {{")
    lines.append(f"\treturn &{schema}Store{{data: make(map[string]{schema})}}")
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
    lines.append(f"func (s *{schema}Store) Find(id string) *{schema} {{")
    lines.append("\ts.mu.RLock(); defer s.mu.RUnlock()")
    lines.append("\tp, ok := s.data[id]")
    lines.append("\tif !ok { return nil }")
    lines.append("\treturn &p")
    lines.append("}")
    lines.append("")

    # Insert
    lines.append(f"func (s *{schema}Store) Insert(dto {schema}CreateDTO) {schema} {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\tp := {schema}{{")
    lines.append(f'\t\tId: uuid.New().String(),')
    for field_name in dto_fields:
        prop = pascal_case(field_name)
        lines.append(f"\t\t{prop}: dto.{prop},")
    lines.append("\t}")
    lines.append("\ts.data[p.Id] = p")
    lines.append("\treturn p")
    lines.append("}")
    lines.append("")

    # Update
    lines.append(f"func (s *{schema}Store) Update(id string, dto {schema}CreateDTO) *{schema} {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append("\tif _, ok := s.data[id]; !ok { return nil }")
    lines.append(f"\tp := {schema}{{Id: id,")
    for field_name in dto_fields:
        prop = pascal_case(field_name)
        lines.append(f"\t\t{prop}: dto.{prop},")
    lines.append("\t}")
    lines.append("\ts.data[id] = p")
    lines.append("\treturn &p")
    lines.append("}")
    lines.append("")

    # Delete
    lines.append(f"func (s *{schema}Store) Delete(id string) bool {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append("\tif _, ok := s.data[id]; !ok { return false }")
    lines.append("\tdelete(s.data, id)")
    lines.append("\treturn true")
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
        if is_pg:
            lines.append(f"{pad}{inst['assign']}, err := {store}.Find(ctx, {inst['key']})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{inst['assign']} := {store}.Find({inst['key']})")

    elif op == 'STORE_FIND_BY':
        lines.extend(_compile_store_find_by_go(inst, indent, ctx))

    elif op == 'STORE_FILTER':
        lines.extend(_compile_store_filter_go(inst, indent, ctx))

    elif op == 'STORE_INSERT':
        if is_pg:
            lines.append(f"{pad}{inst['assign']}, err := {store}.Insert(ctx, {inst['data']})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{inst['assign']} := {store}.Insert({inst['data']})")

    elif op == 'STORE_UPDATE':
        if is_pg:
            lines.append(f"{pad}{inst['assign']}, err := {store}.Update(ctx, {inst['key']}, {inst['data']})")
            lines.append(f"{pad}if err != nil {{ respondError(w, ErrInternal(err.Error())); return }}")
        else:
            lines.append(f"{pad}{inst['assign']} := {store}.Update({inst['key']}, {inst['data']})")

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
            args = [_go_value(params.get(p, "")) for p in declared]
            lines.append(f'{pad}respondError(w, {fname}({", ".join(args)}))')
        else:
            tag = err_type_tag(err_type)
            lines.append(f'{pad}respondError(w, ErrBusinessRule("{tag}", ""))')
        lines.append(f"{pad}return")

    elif op == 'CALL_SERVICE':
        service = inst['service']
        method = inst.get('method', 'GET').upper()
        fmt, args = path_to_go_sprintf(inst.get('path', ''))
        assign = inst['assign']
        if args:
            path_expr = f'fmt.Sprintf("{fmt}", {", ".join(args)})'
        else:
            path_expr = f'"{fmt}"'
        body = inst.get('body')
        if body:
            pairs = ", ".join(f'"{k}": {_go_value(v)}' for k, v in body.items())
            body_expr = f'map[string]any{{{pairs}}}'
        else:
            body_expr = 'nil'
        lines.append(
            f'{pad}{assign} := callService(r.Context(), rt.serviceURLs["{service}"], "{method}", {path_expr}, {body_expr})'
        )
        ctx.setdefault('locals', {})[assign] = 'map'

    elif op == 'EMIT_EVENT':
        lines.append(f'{pad}slog.Info("event", "name", "{inst["event"]}", "data", {inst.get("data", "nil")})')

    elif op == 'LOG':
        lines.append(f'{pad}log.Printf("[{inst.get("level", "INFO")}] {inst["message"]}")')

    # ── Phase 1: Variables and expressions ──
    elif op == 'DEFINE_VAR':
        lines.extend(_compile_define_var_go(inst, pad, ctx))

    elif op == 'SET_VAR':
        lines.append(f"{pad}{inst['name']} = {_go_value(inst.get('value'))}")

    elif op == 'COMPUTE':
        left = _go_value(inst.get('left'))
        right = _go_value(inst.get('right'))
        operator = inst.get('operator', '+')
        assign = inst['assign']
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
            lines.append(f"{pad}{target} = append({target}, {_go_value(inst['value'])})")
        else:
            locals_dict = ctx.get('locals', {}) or {}
            ttype = locals_dict.get(target, 'List<object>')
            elem = extract_list_elem(ailang_type_to_go(ttype))
            fields = inst.get('object_fields', {})
            init = ", ".join(f"{pascal_case(k)}: {_go_value(v)}" for k, v in fields.items())
            lines.append(f"{pad}{target} = append({target}, {elem}{{{init}}})")

    elif op == 'ITERATE':
        collection = inst['collection']
        item = inst['item']
        lines.append(f"{pad}for _, {item} := range {collection} {{")
        inner_ctx = dict(ctx)
        inner_ctx['locals'] = dict(ctx.get('locals', {}))
        inner_ctx['locals'][item] = 'object'
        for sub in inst.get('body', []):
            lines.extend(compile_instruction_go(sub, indent + 1, inner_ctx))
        lines.append(f"{pad}}}")

    elif op == 'CONSTRUCT':
        schema = inst['schema']
        fields = inst.get('fields', {})
        parts = [f"{pascal_case(k)}: {_go_value(v)}" for k, v in fields.items()]
        body = ", ".join(parts)
        lines.append(f"{pad}{inst['assign']} := {schema}{{{body}}}")
        ctx.setdefault('locals', {})[inst['assign']] = schema

    elif op == 'GET_CURRENT':
        from_ = inst.get('from', 'auth')
        field = inst.get('field', 'user')
        assign = inst['assign']
        if from_ == 'auth' and field == 'user':
            # Strip "Bearer " and call the token as the subject — placeholder
            lines.append(f'{pad}{assign} := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")')
        elif from_ == 'clock' and field == 'now':
            lines.append(f"{pad}{assign} := time.Now().UTC()")
        elif from_ == 'clock' and field == 'today':
            lines.append(f"{pad}{assign} := time.Now().UTC().Truncate(24 * time.Hour)")
        elif from_ == 'clock' and field == 'day_of_week':
            lines.append(f"{pad}{assign} := time.Now().UTC().Weekday()")
        else:
            lines.append(f'{pad}var {assign} any  // GET_CURRENT {from_}.{field} (unsupported)')

    elif op == 'MATCH':
        lines.extend(compile_match_go(inst, indent, ctx))

    elif op == 'BRANCH':
        lines.extend(compile_branch_go(inst, indent, ctx))

    return lines


def _go_value(v) -> str:
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
        return [f"{pad}var {name} {go_type} = {_go_value(value)}"]
    if vtype == 'object' and s.startswith('{'):
        return [f"{pad}var {name} {go_type}  // TODO: object initializer {s}"]
    return [f"{pad}var {name} {go_type} = {s}"]


def _compile_compute_fn_go(inst: dict, pad: str, ctx: dict) -> list[str]:
    fn = inst['function']
    args = [_go_value(a) for a in inst.get('args', [])]
    assign = inst['assign']
    fn_map = {
        'round': 'math.Round', 'floor': 'math.Floor', 'ceil': 'math.Ceil',
        'min': 'min', 'max': 'max', 'abs': 'math.Abs',
    }
    if fn == 'length':
        src = args[0] if args else 'nil'
        return [f"{pad}{assign} := len({src})"]
    if fn in fn_map:
        return [f"{pad}{assign} := {fn_map[fn]}({', '.join(args)})"]
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
    value = inst['value']
    cases = inst.get('cases', {})

    if 'OK' in cases and 'NONE' in cases:
        # OK + NONE → pointer/nullable check (store.Find pattern)
        lines.append(f"{pad}if {value} == nil {{")
        for sub in cases['NONE']:
            lines.extend(compile_instruction_go(sub, indent + 1, ctx))
        lines.append(f"{pad}}}")
        for sub in cases['OK']:
            lines.extend(compile_instruction_go(sub, indent, ctx))
    elif 'OK' in cases and 'ERR' in cases:
        # OK + ERR → value-type MATCH after READ_BODY. Decode errors were already
        # handled by READ_BODY, so the ERR branch is dead code in Go (structs
        # can't be compared to nil). Emit the OK branch inline only.
        for sub in cases['OK']:
            lines.extend(compile_instruction_go(sub, indent, ctx))

    return lines


def compile_branch_go(inst: dict, indent: int, ctx: dict = None) -> list[str]:
    pad = "\t" * indent
    lines = []
    condition = inst.get('condition', '')

    if isinstance(condition, dict):
        left = _go_value(condition.get('left', ''))
        op = condition.get('op', '==')
        right = _go_value(condition.get('right', ''))
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
# MAIN
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print("AILang Transpiler v0.1")
        print("Usage: python transpiler.py <bytecode.json> [--target csharp|go|all]")
        sys.exit(1)

    filepath = sys.argv[1]
    target = "all"

    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]

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

    if target in ('csharp', 'all'):
        output_dir = os.path.join(base_dir, f"generated-csharp-{service_name}")
        generate_csharp(bc, output_dir)

    if target in ('go', 'all'):
        output_dir = os.path.join(base_dir, f"generated-go-{service_name}")
        generate_go(bc, output_dir)

    print()
    print("  Done!")


if __name__ == "__main__":
    main()
