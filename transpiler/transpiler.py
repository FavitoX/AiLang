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
        assign = inst['assign']
        data = inst['data']
        locals_dict = ctx.setdefault('locals', {})
        # If the data var holds a full schema type (from CONSTRUCT) instead of a
        # DTO, the store's Insert(Dto) won't accept it. In that case skip the
        # insert (the entity is already fully constructed) and just alias.
        data_type = locals_dict.get(data)
        store_schema = (ctx.get('store_def') or {}).get('schema', '')
        if data_type == store_schema and data_type:
            # Already a full entity — just assign (re-use the constructed value).
            if assign in locals_dict:
                lines.append(f"{pad}{assign} = {data};")
            else:
                lines.append(f"{pad}var {assign} = {data};")
                locals_dict[assign] = data_type
        elif is_pg:
            lines.append(f"{pad}var {assign} = await {store_var}.InsertAsync({data});")
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
        # AILang contract: READ_BODY runs schema validations (required/min/max/gt/…)
        # automatically. Generator enforces this by calling Validate() right after
        # JSON decode — mirrors the behavior described in CLAUDE.md.
        lines.append(f"{pad}var {inst['assign']} = await ctx.Request.ReadFromJsonAsync<{inst['schema']}Dto>();")
        lines.append(f"{pad}if ({inst['assign']} is null)")
        lines.append(f"{pad}    return Results.BadRequest(AiLangError.InvalidBody(\"{inst['schema']}\", \"body is null\"));")
        lines.append(f"{pad}{{")
        lines.append(f"{pad}    var _verr = {inst['assign']}.Validate();")
        lines.append(f"{pad}    if (_verr != null) return Results.BadRequest(_verr);")
        lines.append(f"{pad}}}")

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
            # Use _cs_expr so JsonNode-typed roots get indexer access (Phase 3b).
            args = [_cs_expr(params.get(p, ""), ctx) for p in declared]
            lines.append(f'{pad}return Results.Json(AiLangError.{factory}({", ".join(args)}), statusCode: {status});')
        else:
            tag = err_type_tag(err_type)
            lines.append(f'{pad}return Results.Json(AiLangError.BusinessRule("{tag}", ""), statusCode: 422);')

    elif op == 'SAGA_COMPENSATE':
        saga = inst.get('saga', '')
        fn = 'Compensate' + pascal_case(saga) + 'Async'
        lines.append(f"{pad}await {fn}();")

    elif op == 'PARALLEL':
        lines.extend(_compile_parallel_csharp(inst, indent, ctx))

    elif op == 'AWAIT_ALL':
        lines.extend(_compile_await_all_csharp(inst, indent, ctx))

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
        lines.append(f"{pad}{name} = {_cs_expr(inst.get('value'), ctx, hint=hint)};")

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
        # Phase 7b: iterating a JsonNode array requires .AsArray() — foreach on
        # JsonNode? is a type error. Also propagate the element type to inner
        # locals so field access inside the body routes through JsonNode indexing.
        coll_type = (ctx.get('locals') or {}).get(collection) if ctx else None
        if coll_type == 'JsonNode':
            lines.append(f"{pad}foreach (var {item} in {collection}!.AsArray())")
            item_type = 'JsonNode'
        else:
            lines.append(f"{pad}foreach (var {item} in {collection})")
            item_type = 'object'
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
            # Anonymous object — C# anonymous type (boxed as object).
            # Bare atoms that aren't known locals are treated as string literals
            # (the converter emits them unquoted from the JSON source).
            known = (ctx.get('locals') or {})
            parts = []
            for k, v in fields.items():
                val = _cs_expr(v, ctx)
                if isinstance(v, str) and v not in known and not v.startswith('"') and '.' not in v:
                    try: float(v)  # keep numbers
                    except ValueError: val = f'"{v}"'
                parts.append(f"{pascal_case(k)} = {val}")
            body = ", ".join(parts)
            locals_dict = ctx.setdefault('locals', {})
            if assign in locals_dict:
                lines.append(f"{pad}{assign} = (object)new {{ {body} }};")
            else:
                lines.append(f"{pad}object {assign} = new {{ {body} }};")
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
            lines.append(f"{pad}var {assign} = DateTime.UtcNow;")
        elif from_ == 'clock' and field in ('today', 'day_of_week'):
            call = 'DateTime.UtcNow.Date' if field == 'today' else 'DateTime.UtcNow.DayOfWeek'
            lines.append(f"{pad}var {assign} = {call};")
        else:
            lines.append(f"{pad}var {assign} = default!;  // GET_CURRENT {from_}.{field} (unsupported)")

    elif op == 'LOG':
        level = inst.get('level', 'INFO')
        import re
        msg = inst['message']
        # PascalCase member access segments inside {interpolation} braces.
        msg = re.sub(r'\{(\w+(?:\.\w+)+)\}', lambda m: '{' + m.group(1).split('.')[0] + ''.join('.' + pascal_case(s) for s in m.group(1).split('.')[1:]) + '}', msg)
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
    # Typed access — same rule as before.
    return parts[0] + ''.join('.' + pascal_case(p) for p in parts[1:])


def _cs_expr(v, ctx: dict = None, hint: str = None) -> str:
    """Context-aware value formatter. Delegates to _cs_value for scalars/literals
    but routes dotted identifiers through _cs_resolve_path so JsonNode-typed
    roots get indexer access instead of pascalized member access."""
    from sexpr_parser import StringLiteral
    if ctx and isinstance(v, str) and not isinstance(v, StringLiteral) and '.' in v:
        # Only treat as identifier if the chars look like one (no quoting etc).
        s = v
        if s and (s[0].isalpha() or s[0] == '_') and all(c.isalnum() or c in '_.' for c in s):
            return _cs_resolve_path(s, ctx, hint)
    return _cs_value(v)


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
    if vtype == 'string':
        return [f"{pad}{cs_type} {name} = {_cs_value(value)};"]
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
    # Build the tuple. Named-tuple fields match fiber names verbatim so the
    # member-access idiom `parallel_results.fetch_products` just works.
    tuple_fields = ", ".join(
        f"{fname}: ({tv}.Status == TaskStatus.RanToCompletion ? {tv}.Result : null)"
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
    """Compile MATCH instruction to C# if/else.

    The value is used verbatim — C# tuples permit snake_case field names, so
    `parallel_results.fetch_products` already resolves correctly. Only Go
    requires PascalCasing of trailing segments (struct fields must be exported).

    Phase 7b: when matching `{source}.{fiber}` and the source is a PARALLEL
    result, the OK branch rebinds the fiber's in-fiber result_var to the tuple
    field so downstream body code (which was written against the fiber-local
    name) still resolves. Also registers the rebound name as JsonNode.
    """
    pad = "    " * indent
    lines = []
    value = inst['value']
    cases = inst.get('cases', {})

    # Detect PARALLEL-result MATCH: lookup in ctx['_parallel_tasks'] by source name.
    rebind_stmt = None
    rebind_var = None
    if isinstance(value, str) and '.' in value:
        source, _, fname = value.partition('.')
        fibers = ((ctx or {}).get('_parallel_tasks') or {}).get(source) or []
        for fn, _tv, rv in fibers:
            if fn == fname and rv:
                rebind_var = rv
                rebind_stmt = f"var {rv} = {value};"
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
        # Infer value hint from arm key types: all-int → int, else string.
        all_int = all(isinstance(k, int) or (isinstance(k, str) and k.isdigit()) for k in cases if str(k) != 'DEFAULT')
        value_hint = 'int' if all_int else 'string'
        value_expr = _cs_expr(value, ctx, hint=value_hint)
        first = True
        for arm_key, arm_body in cases.items():
            arm_str = str(arm_key)
            if arm_str == 'DEFAULT':
                lines.append(f"{pad}else")
            else:
                arm_val = _cs_value(arm_key)
                if isinstance(arm_key, str) and arm_key not in known and not arm_key.startswith('"'):
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
        if hint_left is None and _is_json_root(left_raw) and isinstance(right_raw, str):
            hint_left = 'string'
        if hint_right is None and _is_json_root(right_raw) and isinstance(left_raw, str):
            hint_right = 'string'
        left = _cs_expr(left_raw, ctx, hint=hint_left)
        op = condition.get('op', '==')
        right = _cs_expr(right_raw, ctx, hint=hint_right)
        # Quote bare-atom rhs that isn't a known local — it's a literal value.
        if isinstance(right_raw, str) and right_raw not in ((ctx.get('locals') or {})) and not right_raw.startswith('"') and '.' not in right_raw:
            try: float(right_raw)
            except ValueError:
                if right_raw.lower() not in ('true', 'false'):
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
        }
        for inst in instructions:
            lines.extend(compile_instruction_csharp(inst, 1, ctx))

        # Safety return — guarantees the lambda always returns an IResult so C#
        # doesn't complain about missing return paths in complex MATCH/BRANCH trees.
        lines.append("    return Results.StatusCode(500); // unreachable — safety fallback")
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

    # Event bus types (emitted AFTER app.Run since they are type declarations).
    if uses_events_cs:
        lines.append("")
        lines.extend(generate_event_bus_csharp())

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

    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
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
    # Cast eventData to JsonNode so `event.data.X` paths resolve via Phase 3b
    # indexer access. AILang's `event.data` IS the full payload — the `.data`
    # segment is stripped by registering the root as `event.data` → `_evtData`.
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
    lines = [f'{pad}.AddResilienceHandler("{handler}", b =>', f"{pad}{{"]
    cb = policy.get('circuit_breaker')
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
    lines.append("    void Reset();  // test seam — RESET_STORE in (tests ... (clean ...))")
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
    lines.append("    public void Reset() => _data.Clear();")
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
    needs_math = 'COMPUTE_FN' in all_ops
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
    if not is_pg:
        for store_name, store_def in bc.stores.items():
            lines.append(f"\tstore := New{store_def['schema']}Store()")
    if uses_call_service_anywhere:
        lines.append("\tserviceURLs := map[string]string{")
        for svc_name, svc_def in service_refs_go.items():
            env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
            default_url = svc_def.get('default', 'http://localhost')
            lines.append(f'\t\t"{svc_name}": func() string {{ if v := os.Getenv("{env_var}"); v != "" {{ return v }}; return "{default_url}" }}(),')
        lines.append("\t}")
    if uses_events_go:
        lines.append("\tbus := NewEventBus()")
        for h_name, h_def in event_handlers_go:
            evt = h_def.get('event') or h_name
            fn_name = pascal_case(h_name) if h_name.startswith('on_') else 'On' + pascal_case(h_name)
            lines.append(f'\tbus.Subscribe("{evt}", {fn_name})')
    router_fields = []
    if bc.stores and not is_pg:
        router_fields.append("store: store")
    if uses_call_service_anywhere:
        router_fields.append("serviceURLs: serviceURLs")
    if uses_events_go:
        router_fields.append("bus: bus")
    lines.append(f"\trouter := &Router{{{', '.join(router_fields)}}}")
    lines.append("\treturn corsMiddleware(tracingMiddleware(router.mux()))")
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
        # PG path keeps the old inline wiring — buildHandler() is in-memory only.
        if uses_call_service_anywhere:
            lines.append("\tserviceURLs := map[string]string{")
            for svc_name, svc_def in service_refs_go.items():
                env_var = svc_def.get('base_url_ref', '').replace('ENV.', '') or 'UNSET'
                default_url = svc_def.get('default', 'http://localhost')
                lines.append(f'\t\t"{svc_name}": func() string {{ if v := os.Getenv("{env_var}"); v != "" {{ return v }}; return "{default_url}" }}(),')
            lines.append("\t}")
        if uses_events_go:
            lines.append("\tbus := NewEventBus()")
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
        lines.append("\thandler := corsMiddleware(tracingMiddleware(router.mux()))")
    else:
        lines.append("\thandler := buildHandler()")
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
    if needs_resilience_go:
        requires.append('\tgithub.com/sony/gobreaker/v2 v2.0.0')
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
        'locals': {'data': 'any'},
    }
    for inst in handler_def.get('instructions', []):
        lines.extend(compile_instruction_go(inst, 1, ctx))
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
            "func callService(ctx context.Context, svcName, baseURL, method, path string, body any, _idempotent bool) any {",
            "\tvar reqBody io.Reader",
            "\tif body != nil {",
            "\t\tb, err := json.Marshal(body)",
            '\t\tif err != nil { slog.Error("call_service marshal", "err", err); return nil }',
            "\t\treqBody = bytes.NewReader(b)",
            "\t}",
            "\treq, err := http.NewRequestWithContext(ctx, method, baseURL+path, reqBody)",
            '\tif err != nil { slog.Error("call_service req", "err", err); return nil }',
            '\tif body != nil { req.Header.Set("Content-Type", "application/json") }',
            "\tresp, err := _httpClient.Do(req)",
            '\tif err != nil { slog.Error("call_service do", "err", err, "svc", svcName); return nil }',
            "\tdefer resp.Body.Close()",
            "\tif resp.StatusCode >= 400 {",
            '\t\tslog.Info("call_service non-2xx", "status", resp.StatusCode, "path", path)',
            "\t\treturn nil",
            "\t}",
            "\tvar out any",
            "\tif err := json.NewDecoder(resp.Body).Decode(&out); err != nil {",
            '\t\tslog.Error("call_service decode", "err", err)',
            "\t\treturn nil",
            "\t}",
            "\treturn out",
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
        "// per-attempt timeout, and retry-with-backoff. Returns nil on final failure.",
        "func callService(ctx context.Context, svcName, baseURL, method, path string, body any, idempotent bool) any {",
        "\tif len(_cbs) > 0 { _cbsOnce.Do(_initCBs) }",
        '\tpolicy := _svcPolicies[svcName]',
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
        "\tfor attempt := 0; attempt < maxAttempts; attempt++ {",
        "\t\tif attempt > 0 {",
        "\t\t\tdelay := initial",
        "\t\t\tif expo { delay = initial << (attempt - 1) }",
        "\t\t\tif maxBackoff > 0 && delay > maxBackoff { delay = maxBackoff }",
        "\t\t\tselect {",
        "\t\t\tcase <-time.After(delay):",
        "\t\t\tcase <-ctx.Done():",
        "\t\t\t\treturn nil",
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
        "\t\t\tbreak  // success",
        "\t\t}",
        '\t\tslog.Info("call_service attempt failed", "svc", svcName, "attempt", attempt+1, "err", err.Error())',
        "\t}",
        "\tif lastBody == nil { return nil }",
        "\tvar out any",
        "\tif err := json.Unmarshal(lastBody, &out); err != nil {",
        '\t\tslog.Error("call_service decode", "err", err)',
        "\t\treturn nil",
        "\t}",
        "\treturn out",
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
    lines.append("")

    # Reset — test seam for RESET_STORE in (tests ... (clean ...))
    lines.append(f"func (s *{schema}Store) Reset() {{")
    lines.append("\ts.mu.Lock(); defer s.mu.Unlock()")
    lines.append(f"\ts.data = make(map[string]{schema})")
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

    elif op == 'SAGA_COMPENSATE':
        saga = inst.get('saga', '')
        fn = 'compensate' + pascal_case(saga)
        lines.append(f"{pad}{fn}(r.Context())")

    elif op == 'PARALLEL':
        lines.extend(_compile_parallel_go(inst, indent, ctx))

    elif op == 'AWAIT_ALL':
        lines.extend(_compile_await_all_go(inst, indent, ctx))

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
        idempotent = 'true' if inst.get('idempotent', True) else 'false'
        lines.append(
            f'{pad}{assign} := callService(r.Context(), "{service}", rt.serviceURLs["{service}"], "{method}", {path_expr}, {body_expr}, {idempotent})'
        )
        # callService returns `any` — objects decode as map[string]any, arrays as
        # []any. Register as 'any_json' so downstream knows to assert per context.
        ctx.setdefault('locals', {})[assign] = 'any_json'

    elif op == 'EMIT_EVENT':
        data_expr = _go_value(inst.get('data')) if inst.get('data') not in (None, '') else 'nil'
        lines.append(f'{pad}rt.bus.Publish(r.Context(), "{inst["event"]}", {data_expr})')

    elif op == 'LOG':
        lines.append(f'{pad}log.Printf("[{inst.get("level", "INFO")}] {inst["message"]}")')

    # ── Phase 1: Variables and expressions ──
    elif op == 'DEFINE_VAR':
        lines.extend(_compile_define_var_go(inst, pad, ctx))

    elif op == 'SET_VAR':
        name = inst['name']
        hint = (ctx.get('locals') or {}).get(name)
        lines.append(f"{pad}{name} = {_go_expr(inst.get('value'), ctx, hint=hint)}")

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
        # Phase 7b: JsonNode-like collections (from CALL_SERVICE) are decoded as
        # map[string]any. To iterate as a list, cast the raw slice first. The
        # emitted `_<coll>_arr` is a temporary []any slice; `item` inside the
        # body becomes one element, registered as `map` so Phase 3b kicks in.
        coll_type = (ctx.get('locals') or {}).get(collection) if ctx else None
        if coll_type in ('map', 'any_json'):
            arr_var = f"_{collection}_arr"
            lines.append(f'{pad}{arr_var}, _ := {collection}.([]any)')
            lines.append(f"{pad}for _, _elem := range {arr_var} {{")
            lines.append(f'{pad}\t{item}, _ := _elem.(map[string]any)')
            item_type = 'any_json'
        else:
            lines.append(f"{pad}for _, {item} := range {collection} {{")
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
            known = (ctx.get('locals') or {})
            parts = []
            for k, v in fields.items():
                val = _go_expr(v, ctx)
                if isinstance(v, str) and v not in known and not v.startswith('"') and '.' not in v:
                    try: float(v)
                    except ValueError: val = f'"{v}"'
                parts.append(f'"{k}": {val}')
            body = ", ".join(parts)
            locals_dict = ctx.setdefault('locals', {})
            if assign in locals_dict:
                lines.append(f"{pad}{assign} = map[string]any{{{body}}}")
            else:
                lines.append(f"{pad}{assign} := map[string]any{{{body}}}")
                locals_dict[assign] = 'object'
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
        if from_ == 'auth' and field == 'user':
            lines.append(f'{pad}{assign} := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")')
        elif from_ == 'clock' and field == 'now':
            lines.append(f"{pad}{assign} := time.Now().UTC()")
        elif from_ == 'clock' and field == 'today':
            lines.append(f"{pad}{assign} := time.Now().UTC().Truncate(24 * time.Hour)")
        elif from_ == 'clock' and field == 'day_of_week':
            lines.append(f"{pad}{assign} := time.Now().UTC().Weekday()")
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
    base = _go_value(v)
    # Widen plain identifiers whose known type is int when used in a decimal
    # context (Go has no implicit numeric conversions).
    if hint == 'decimal' and ctx and isinstance(v, str):
        known_type = (ctx.get('locals') or {}).get(v)
        if known_type == 'int':
            return f'float64({base})'
    return base


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
    raw_args = inst.get('args', [])
    args = [_go_value(a) for a in raw_args]
    assign = inst['assign']
    locals_dict = ctx.setdefault('locals', {})
    decl = f"{assign} =" if assign in locals_dict else f"{assign} :="
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
        # OK + ERR → value-type MATCH after READ_BODY. Decode errors were already
        # handled by READ_BODY, so the ERR branch is dead code in Go (structs
        # can't be compared to nil). Emit the OK branch inline only.
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
                if isinstance(arm_key, str) and arm_key not in known and not arm_key.startswith('"'):
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
        left = _go_expr(left_raw, ctx, hint=hint_left)
        op = condition.get('op', '==')
        right = _go_expr(right_raw, ctx, hint=hint_right)
        if op == 'NOT_IN':
            cond_str = f"!_sliceContains({right}, {left})"
        elif op == 'IN':
            cond_str = f"_sliceContains({right}, {left})"
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
    The returned string is a JSON document (no surrounding C#/Go quoting)."""
    parts = []
    for pair in body_form:
        if not (isinstance(pair, list) and len(pair) >= 2):
            continue
        key = str(pair[0])
        val = pair[1]
        if isinstance(val, bool):
            parts.append(f'"{key}":{"true" if val else "false"}')
        elif isinstance(val, (int, float)):
            parts.append(f'"{key}":{val}')
        elif isinstance(val, str) and val.lower() in ('true', 'false'):
            parts.append(f'"{key}":{val.lower()}')
        else:
            # Escape embedded quotes & backslashes for JSON safety.
            s = str(val).replace('\\', '\\\\').replace('"', '\\"')
            parts.append(f'"{key}":"{s}"')
    return '{' + ','.join(parts) + '}'


def _assert_is_length_op(target: str) -> bool:
    return target.endswith('.length') or target == 'length'


def _split_json_path(target: str) -> list[str]:
    """'body.error.type' → ['error', 'type']. Strips the leading 'body.'."""
    segs = target.split('.')
    if segs and segs[0] == 'body':
        segs = segs[1:]
    return segs


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
    - None / empty → `"{}"` (caller picks array/object context elsewhere if needed).
    """
    if not body:
        return "{}"
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
                lines.append(f'{pad}Assert.True(_body.GetArrayLength() {cs_cmp} {val}, $"body.length = {{_body.GetArrayLength()}}");')
            else:
                segs = _split_json_path(target)
                walk = '_body'
                for s in segs:
                    walk += f'.GetProperty("{s}")'
                expected = op[2] if len(op) > 2 else None
                expected_str = str(expected).strip('"').lower() if expected is not None else ''
                if str(expected) == 'exists':
                    probe = '_body'
                    for s in segs[:-1]:
                        probe += f'.GetProperty("{s}")'
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
                lines.append(f'{pad}if !(len(_bodyArr) {cmp_op} {val}) {{ t.Errorf("body.length = %d, want {cmp_op} {val}", len(_bodyArr)) }}')
            else:
                segs = _split_json_path(target)
                walk = '_body'
                for s in segs[:-1]:
                    walk = f'({walk}["{s}"].(map[string]any))'
                last = segs[-1]
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
            walk = '_body'
            for s in segs[:-1]:
                walk = f'({walk}["{s}"].(map[string]any))'
            last = segs[-1]
            # Assign to the hoisted outer variable (type already declared as string).
            lines.append(f'{pad}if _v, _ok := {walk}["{last}"].(string); _ok {{ {var} = _v }}')
    return lines


def generate_csharp_tests(bc, main_output_dir: str):
    """Emit an xUnit test project that exercises the generated C# API.

    Structure: sibling `Tests/` folder inside `main_output_dir`. The test project
    references the main .csproj by relative path. One `[Fact]` per `(test ...)`.
    """
    if not bc.tests:
        return
    # Postgres-backed services need testcontainers / a live DB — out of scope here.
    pg_stores = {n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'}
    if pg_stores:
        print("  ⤷ skipping xUnit test project (PostgreSQL store — post-MVP)")
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
        lines.append("    private readonly (string Method, string Path, int Status, string Body)[] _rules;")
        lines.append("    public MockHttpHandler((string, string, int, string)[] rules) { _rules = rules; }")
        lines.append("    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)")
        lines.append("    {")
        lines.append("        var method = request.Method.Method;")
        lines.append("        var path = request.RequestUri!.AbsolutePath;")
        lines.append("        foreach (var rule in _rules)")
        lines.append("        {")
        lines.append("            if (rule.Method == method && rule.Path == path)")
        lines.append("            {")
        lines.append("                var resp = new HttpResponseMessage((HttpStatusCode)rule.Status);")
        lines.append('                resp.Content = new StringContent(rule.Body, Encoding.UTF8, "application/json");')
        lines.append("                return Task.FromResult(resp);")
        lines.append("            }")
        lines.append("        }")
        lines.append("        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound));")
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
        captures: set = set()
        lines.append("    [Fact]")
        lines.append(f"    public async Task {ident}()")
        lines.append("    {")
        # When a test declares mocks, derive a factory with overridden HttpClient
        # handlers and bind _client to the derived factory's CreateClient().
        # Without mocks we keep using the shared field _client from the ctor.
        client_var = "_client"
        if mocks:
            lines.append("        var _mockFactory = _factory.WithWebHostBuilder(builder =>")
            lines.append("        {")
            lines.append("            builder.ConfigureTestServices(services =>")
            lines.append("            {")
            for svc, endpoints in mocks.items():
                rules_cs = []
                for ep in endpoints:
                    body_json = _mock_body_to_json(ep.get('body'))
                    body_escaped = body_json.replace('\\', '\\\\').replace('"', '\\"')
                    rules_cs.append(f'("{ep["method"]}", "{ep["path"]}", {ep["status"]}, "{body_escaped}")')
                rules_join = ",\n                        ".join(rules_cs)
                lines.append(f'                services.AddHttpClient("{svc}")')
                lines.append("                    .ConfigurePrimaryHttpMessageHandler(() => new MockHttpHandler(new (string, string, int, string)[]")
                lines.append("                    {")
                lines.append("                        " + rules_join)
                lines.append("                    }));")
                # Also override the _no_retry sibling if the main codegen registered one
                # (resilience policies with retry emit it). Safe to always register — if
                # the service has no such client, AddHttpClient just creates it harmlessly.
                lines.append(f'                services.AddHttpClient("{svc}_no_retry")')
                lines.append("                    .ConfigurePrimaryHttpMessageHandler(() => new MockHttpHandler(new (string, string, int, string)[]")
                lines.append("                    {")
                lines.append("                        " + rules_join)
                lines.append("                    }));")
            lines.append("            });")
            lines.append("        });")
            lines.append("        using var _mockedClient = _mockFactory.CreateClient();")
            client_var = "_mockedClient"
        # Swap the effective client name used by the compiled run ops.
        if client_var != "_client":
            lines.append(f"        var _client = {client_var};  // shadow outer field for this test")
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


def generate_go_tests(bc, output_dir: str):
    """Emit main_test.go using httptest.NewServer(buildHandler())."""
    if not bc.tests:
        return
    pg_stores = {n: s for n, s in bc.stores.items() if s.get('provider') == 'PostgreSQL'}
    if pg_stores:
        print("  ⤷ skipping Go httptest (PostgreSQL store — post-MVP)")
        return

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
    # silence-unused to keep imports in single-test edge cases
    lines.append("var _ = strings.HasPrefix")
    lines.append("var _ = fmt.Sprintf")
    lines.append("var _ io.Reader = nil")
    lines.append("var _ = json.Unmarshal")
    lines.append("")

    for test in bc.tests:
        ident = 'Test' + pascal_case(_test_name_to_ident(test['name']))
        run_ops = test.get('run') or []
        mocks = test.get('mock_services') or {}
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
                lines.append(f'\t\tif r.Method == "{ep["method"]}" && r.URL.Path == "{ep["path"]}" {{')
                lines.append(f'\t\t\tw.WriteHeader({ep["status"]})')
                lines.append(f'\t\t\tw.Write([]byte(`{json_body}`))')
                lines.append('\t\t\treturn')
                lines.append('\t\t}')
            lines.append("\t\thttp.NotFound(w, r)")
            lines.append("\t}))")
            lines.append(f"\tdefer {mock_var}.Close()")
            lines.append(f'\tt.Setenv("{env_var}", {mock_var}.URL)')
        lines.append("\tts := newTestServer(t)")
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
        generate_csharp_tests(bc, output_dir)

    if target in ('go', 'all'):
        output_dir = os.path.join(base_dir, f"generated-go-{service_name}")
        generate_go(bc, output_dir)
        generate_go_tests(bc, output_dir)

    print()
    print("  Done!")


if __name__ == "__main__":
    main()
