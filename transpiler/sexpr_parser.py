"""
S-expression Parser for AILang Bytecode

Parses .sexpr files into nested Python lists and atoms.
Example:
    (handler get_user (auth PUBLIC) (do (RESPOND 200 user)))
    
    becomes:
    ['handler', 'get_user', ['auth', 'PUBLIC'], ['do', ['RESPOND', '200', 'user']]]
"""

from typing import Union

# An Atom is a string, int, or float
Atom = Union[str, int, float]
# An SExpr is either an Atom or a list of SExpr
SExpr = Union[Atom, list]


class StringLiteral(str):
    """Marker subclass: atoms that came from a `"..."` quoted source token.

    Still a regular `str` (compares equal, serializes to JSON as a plain string),
    but `isinstance(x, StringLiteral)` lets the code generators distinguish a
    quoted string literal from a bare identifier or symbol — critical for places
    like `(DEFINE_VAR x string "cancelled")` or `(!= status "pending")` where
    the generator needs to emit a quoted literal, not a variable reference.
    """
    __slots__ = ()


class ParseError(Exception):
    def __init__(self, message: str, line: int = 0, col: int = 0):
        self.line = line
        self.col = col
        super().__init__(f"Parse error at line {line}, col {col}: {message}")


def tokenize(source: str) -> list[str]:
    """Split source into tokens: (, ), strings, and atoms."""
    tokens = []
    i = 0
    line = 1
    
    while i < len(source):
        c = source[i]
        
        # Whitespace
        if c in ' \t\r':
            i += 1
            continue
        
        # Newline
        if c == '\n':
            line += 1
            i += 1
            continue
        
        # Comment (;; to end of line)
        if c == ';':
            while i < len(source) and source[i] != '\n':
                i += 1
            continue
        
        # Open paren
        if c == '(':
            tokens.append('(')
            i += 1
            continue
        
        # Close paren
        if c == ')':
            tokens.append(')')
            i += 1
            continue
        
        # String literal
        if c == '"':
            i += 1
            start = i
            while i < len(source) and source[i] != '"':
                if source[i] == '\\':
                    i += 1  # skip escaped char
                i += 1
            if i >= len(source):
                raise ParseError("unterminated string", line)
            tokens.append('"' + source[start:i] + '"')
            i += 1  # skip closing quote
            continue
        
        # Arrow operator →
        if c == '→' or (c == '-' and i + 1 < len(source) and source[i + 1] == '>'):
            tokens.append('→')
            i += 2 if c == '-' else 1
            # Handle multi-byte → character
            if c == '→':
                # UTF-8 → is 3 bytes but Python handles it as 1 char
                pass
            continue
        
        # Atom (symbol, number, etc.)
        start = i
        while i < len(source) and source[i] not in ' \t\r\n()";':
            # Also stop at → 
            if source[i] == '→':
                break
            if source[i] == '-' and i + 1 < len(source) and source[i + 1] == '>':
                break
            i += 1
        
        atom = source[start:i]
        if atom:
            tokens.append(atom)
        
    return tokens


def parse_tokens(tokens: list[str]) -> list[SExpr]:
    """Parse a flat list of tokens into nested S-expressions."""
    results = []
    i = 0
    
    while i < len(tokens):
        expr, i = parse_expr(tokens, i)
        if expr is not None:
            results.append(expr)
    
    return results


def parse_expr(tokens: list[str], i: int) -> tuple[SExpr, int]:
    """Parse a single expression starting at index i."""
    if i >= len(tokens):
        return None, i
    
    token = tokens[i]
    
    if token == '(':
        # Parse list
        i += 1  # skip (
        items = []
        while i < len(tokens) and tokens[i] != ')':
            expr, i = parse_expr(tokens, i)
            if expr is not None:
                items.append(expr)
        if i < len(tokens):
            i += 1  # skip )
        return items, i
    
    elif token == ')':
        # Unexpected close paren
        return None, i + 1
    
    else:
        # Atom
        return parse_atom(token), i + 1


def parse_atom(token: str) -> Atom:
    """Parse a token into an appropriate Python type."""
    # String literal — wrapped in StringLiteral so the generator can tell it apart
    # from a bare identifier/symbol of the same spelling.
    if token.startswith('"') and token.endswith('"'):
        return StringLiteral(token[1:-1])
    
    # Arrow
    if token == '→':
        return '→'
    
    # Integer
    try:
        return int(token)
    except ValueError:
        pass
    
    # Float
    try:
        return float(token)
    except ValueError:
        pass
    
    # Symbol (string without quotes)
    return token


def parse(source: str) -> list[SExpr]:
    """Parse an S-expression source string into a list of top-level expressions."""
    tokens = tokenize(source)
    return parse_tokens(tokens)


def find_form(exprs: list[SExpr], tag: str) -> SExpr:
    """Find the first top-level form with the given tag.
    E.g., find_form(exprs, 'service') finds (service ...)
    """
    for expr in exprs:
        if isinstance(expr, list) and len(expr) > 0 and expr[0] == tag:
            return expr
    return None


def find_all_forms(exprs: list[SExpr], tag: str) -> list[SExpr]:
    """Find all top-level forms with the given tag."""
    return [e for e in exprs if isinstance(e, list) and len(e) > 0 and e[0] == tag]


def find_prop(form: list, key: str) -> SExpr:
    """Find a property within a form.
    E.g., in (service foo (port 8080) (version "0.7")),
    find_prop(form, 'port') returns ['port', 8080]
    """
    if not isinstance(form, list):
        return None
    for item in form:
        if isinstance(item, list) and len(item) > 0 and item[0] == key:
            return item
    return None


def find_prop_value(form: list, key: str, default=None):
    """Get the value of a property.
    E.g., find_prop_value(service_form, 'port') returns 8080
    """
    prop = find_prop(form, key)
    if prop and len(prop) > 1:
        return prop[1]
    return default


def find_all_props(form: list, key: str) -> list:
    """Find all properties with the given key."""
    if not isinstance(form, list):
        return []
    return [item for item in form if isinstance(item, list) and len(item) > 0 and item[0] == key]


# ── Helpers for extracting handler instructions ──

def extract_instructions(do_block: list) -> list[dict]:
    """Convert a (do ...) block into a list of instruction dicts for the transpiler."""
    if not isinstance(do_block, list) or len(do_block) < 1:
        return []
    
    instructions = []
    items = do_block[1:] if do_block[0] == 'do' else do_block
    
    for item in items:
        if isinstance(item, list) and len(item) > 0:
            inst = sexpr_to_instruction(item)
            if inst:
                instructions.append(inst)
    
    return instructions


def _arrow_idx(expr: list) -> int:
    try:
        return expr.index('→')
    except ValueError:
        return -1


def _split_arrow(expr: list):
    """Return (args_before_arrow, first_atom_after_arrow_or_None)."""
    idx = _arrow_idx(expr)
    if idx < 0:
        return expr[1:], None
    assign = expr[idx + 1] if idx + 1 < len(expr) else None
    return expr[1:idx], assign


def _extract_paginate(args: list) -> dict:
    """Find (paginate (page X) (limit Y) (max_limit Z)) within args, return dict or None."""
    for a in args:
        if isinstance(a, list) and a and a[0] == 'paginate':
            result = {'page': None, 'limit': None, 'max_limit': None}
            for prop in a[1:]:
                if isinstance(prop, list) and len(prop) >= 2:
                    if prop[0] == 'page':
                        result['page'] = prop[1]
                    elif prop[0] == 'limit':
                        result['limit'] = prop[1]
                    elif prop[0] == 'max_limit':
                        result['max_limit'] = prop[1]
            return result
    return None


def _extract_where(args: list):
    """Find (where <expr>) within args, return the raw where expression or None."""
    for a in args:
        if isinstance(a, list) and a and a[0] == 'where':
            return a[1] if len(a) > 1 else None
    return None


def _extract_params(args: list) -> list:
    """Find all (param name type var) within args, return list of dicts."""
    params = []
    for a in args:
        if isinstance(a, list) and a and a[0] == 'param' and len(a) >= 4:
            params.append({'name': a[1], 'type': a[2], 'var': a[3]})
    return params


def sexpr_to_instruction(expr: list) -> dict:
    """Convert a single S-expression instruction to a dict compatible with the transpiler."""
    if not isinstance(expr, list) or len(expr) == 0:
        return None

    op = expr[0]

    # ── STORE operations ──
    if op == 'STORE_LIST_ALL':
        # (STORE_LIST_ALL product_store [(paginate ...)] → result)
        args, assign = _split_arrow(expr)
        store = args[0] if args else None
        paginate = _extract_paginate(args[1:])
        return {'op': 'STORE_LIST_ALL', 'store': store, 'paginate': paginate, 'assign': assign}

    if op == 'STORE_FIND':
        # (STORE_FIND product_store id → product)
        args, assign = _split_arrow(expr)
        return {'op': 'STORE_FIND', 'store': args[0], 'key': args[1], 'assign': assign}

    if op == 'STORE_FIND_BY':
        # (STORE_FIND_BY store (where ...) (param ...) → result)
        args, assign = _split_arrow(expr)
        store = args[0] if args else None
        return {
            'op': 'STORE_FIND_BY',
            'store': store,
            'where': _extract_where(args[1:]),
            'params': _extract_params(args[1:]),
            'assign': assign,
        }

    if op == 'STORE_FILTER':
        # (STORE_FILTER store (where ...) (param ...)+ [(paginate ...)] → result)
        args, assign = _split_arrow(expr)
        store = args[0] if args else None
        return {
            'op': 'STORE_FILTER',
            'store': store,
            'where': _extract_where(args[1:]),
            'params': _extract_params(args[1:]),
            'paginate': _extract_paginate(args[1:]),
            'assign': assign,
        }

    if op == 'STORE_INSERT':
        # (STORE_INSERT product_store body → product)
        args, assign = _split_arrow(expr)
        return {'op': 'STORE_INSERT', 'store': args[0], 'data': args[1], 'assign': assign}

    if op == 'STORE_UPDATE':
        # (STORE_UPDATE product_store id body → updated)
        args, assign = _split_arrow(expr)
        return {
            'op': 'STORE_UPDATE',
            'store': args[0],
            'key': args[1],
            'data': args[2] if len(args) > 2 else None,
            'assign': assign,
        }

    if op == 'STORE_DELETE':
        # (STORE_DELETE product_store id)
        return {'op': 'STORE_DELETE', 'store': expr[1], 'key': expr[2]}

    # ── SQL operations ──
    if op == 'SQL_QUERY':
        # (SQL_QUERY store "SELECT ..." [(param ...)]* → result)
        args, assign = _split_arrow(expr)
        return {
            'op': 'SQL_QUERY',
            'store': args[0] if args else None,
            'sql': args[1] if len(args) > 1 else '',
            'params': _extract_params(args[2:]) if len(args) > 2 else [],
            'assign': assign,
        }

    if op == 'SQL_STREAM':
        # (SQL_STREAM store "SELECT ..." (batch_size N) → stream)
        args, assign = _split_arrow(expr)
        batch_size = None
        for a in args[2:]:
            if isinstance(a, list) and a and a[0] == 'batch_size' and len(a) > 1:
                batch_size = a[1]
        return {
            'op': 'SQL_STREAM',
            'store': args[0] if args else None,
            'sql': args[1] if len(args) > 1 else '',
            'batch_size': batch_size,
            'assign': assign,
        }

    if op == 'SQL_EXEC':
        # (SQL_EXEC store "UPDATE ..." (param ...)* → affected)
        args, assign = _split_arrow(expr)
        return {
            'op': 'SQL_EXEC',
            'store': args[0] if args else None,
            'sql': args[1] if len(args) > 1 else '',
            'params': _extract_params(args[2:]) if len(args) > 2 else [],
            'assign': assign,
        }

    if op == 'STREAM_RESPOND':
        # (STREAM_RESPOND 200 stream (content_type "..."))
        content_type = 'application/json'
        for a in expr[3:]:
            if isinstance(a, list) and a and a[0] == 'content_type' and len(a) > 1:
                content_type = a[1]
        return {
            'op': 'STREAM_RESPOND',
            'status': expr[1] if len(expr) > 1 else 200,
            'stream': expr[2] if len(expr) > 2 else None,
            'content_type': content_type,
        }

    # ── Phase 1: Variables and expressions ──

    if op == 'DEFINE_VAR':
        # (DEFINE_VAR name type value) — value may be a literal or `(list)` for empty list.
        name = expr[1]
        vtype = expr[2] if len(expr) > 2 else 'object'
        value = expr[3] if len(expr) > 3 else None
        # `(list)` → empty list sentinel
        if isinstance(value, list) and value == ['list']:
            value = {'__empty_list__': True}
        return {'op': 'DEFINE_VAR', 'name': name, 'type': vtype, 'value': value}

    if op == 'SET_VAR':
        # (SET_VAR name value)
        return {'op': 'SET_VAR', 'name': expr[1], 'value': expr[2] if len(expr) > 2 else None}

    if op == 'COMPUTE':
        # (COMPUTE left operator right → assign)
        args, assign = _split_arrow(expr)
        return {
            'op': 'COMPUTE',
            'left': args[0] if args else None,
            'operator': args[1] if len(args) > 1 else '+',
            'right': args[2] if len(args) > 2 else None,
            'assign': assign,
        }

    if op == 'COMPUTE_FN':
        # (COMPUTE_FN function arg1 arg2 ... → assign)
        args, assign = _split_arrow(expr)
        fn = args[0] if args else 'identity'
        fn_args = list(args[1:]) if len(args) > 1 else []
        return {'op': 'COMPUTE_FN', 'function': fn, 'args': fn_args, 'assign': assign}

    if op == 'TERNARY':
        # (TERNARY cond then else → assign) — rare as top-level, usually nested
        args, assign = _split_arrow(expr)
        return {
            'op': 'TERNARY',
            'condition': args[0] if args else None,
            'then': args[1] if len(args) > 1 else None,
            'else': args[2] if len(args) > 2 else None,
            'assign': assign,
        }

    if op == 'APPEND':
        # (APPEND target value) — value may be an identifier or (field val)...
        target = expr[1] if len(expr) > 1 else None
        rest = list(expr[2:])
        # If rest is a single atom → append the value directly (simple form)
        # If rest is a list of (field val) forms → construct an object inline
        if len(rest) == 1 and not isinstance(rest[0], list):
            return {'op': 'APPEND', 'target': target, 'value': rest[0]}
        # Multiple (field val) forms → object-construction append
        fields = {}
        for item in rest:
            if isinstance(item, list) and len(item) >= 2:
                fields[item[0]] = item[1]
        return {'op': 'APPEND', 'target': target, 'object_fields': fields}

    if op == 'ITERATE':
        # (ITERATE collection item body_instr body_instr ...)
        collection = expr[1] if len(expr) > 1 else None
        item = expr[2] if len(expr) > 2 else 'item'
        body = [sexpr_to_instruction(i) for i in expr[3:] if isinstance(i, list)]
        body = [b for b in body if b]
        return {'op': 'ITERATE', 'collection': collection, 'item': item, 'body': body}

    if op == 'ITERATE_PARALLEL':
        collection = expr[1] if len(expr) > 1 else None
        item = expr[2] if len(expr) > 2 else 'item'
        body = [sexpr_to_instruction(i) for i in expr[3:] if isinstance(i, list)]
        body = [b for b in body if b]
        return {'op': 'ITERATE_PARALLEL', 'collection': collection, 'item': item, 'body': body}

    if op == 'CONSTRUCT':
        # (CONSTRUCT Schema (field1 val1) (field2 val2) ... → assign)
        args, assign = _split_arrow(expr)
        schema = args[0] if args else None
        fields = {}
        for item in args[1:]:
            if isinstance(item, list) and len(item) >= 2:
                # For nested object values, keep the raw S-expr so the generator can walk it.
                fields[item[0]] = item[1] if len(item) == 2 else item[1:]
        return {'op': 'CONSTRUCT', 'schema': schema, 'fields': fields, 'assign': assign}

    if op == 'GET_CURRENT':
        # (GET_CURRENT from field → assign) — e.g. (GET_CURRENT auth user → u) or (GET_CURRENT clock now → now)
        args, assign = _split_arrow(expr)
        return {
            'op': 'GET_CURRENT',
            'from': args[0] if args else 'auth',
            'field': args[1] if len(args) > 1 else 'user',
            'assign': assign,
        }

    if op == 'IS_OK':
        args, assign = _split_arrow(expr)
        return {'op': 'IS_OK', 'value': args[0] if args else None, 'assign': assign}

    if op == 'SAGA_COMPENSATE':
        # (SAGA_COMPENSATE saga_name)
        return {'op': 'SAGA_COMPENSATE', 'saga': expr[1] if len(expr) > 1 else ''}

    if op == 'CALL_SERVICE':
        # (CALL_SERVICE service_name (method GET) (path "...") (idempotent true) (body ...)? → assign)
        args, assign = _split_arrow(expr)
        service = args[0] if args else None
        method = 'GET'
        path = ''
        idempotent = True
        body = None
        for a in args[1:]:
            if not (isinstance(a, list) and a):
                continue
            key = a[0]
            if key == 'method' and len(a) > 1:
                method = str(a[1])
            elif key == 'path' and len(a) > 1:
                path = a[1]
            elif key == 'idempotent' and len(a) > 1:
                idempotent = a[1] in (True, 'true', 1, '1')
            elif key == 'body':
                fields = {}
                for b in a[1:]:
                    if isinstance(b, list) and len(b) >= 2:
                        fields[b[0]] = b[1]
                body = fields
        return {
            'op': 'CALL_SERVICE',
            'service': service,
            'method': method,
            'path': path,
            'idempotent': idempotent,
            'body': body,
            'assign': assign,
        }
    
    # ── HTTP ──
    if op == 'READ_PARAM':
        # (READ_PARAM id uuid → id)
        name = expr[1]
        ptype = expr[2] if len(expr) > 2 else 'string'
        assign = expr[4] if len(expr) > 4 and expr[3] == '→' else name
        return {'op': 'READ_PARAM', 'name': name, 'type': ptype, 'assign': assign}

    if op == 'READ_QUERY':
        # (READ_QUERY "page" int → page (default 1))
        name = expr[1]
        ptype = expr[2] if len(expr) > 2 else 'string'
        args, assign = _split_arrow(expr)
        default = None
        for a in expr:
            if isinstance(a, list) and a and a[0] == 'default' and len(a) > 1:
                default = a[1]
                break
        if assign is None:
            assign = name
        return {'op': 'READ_QUERY', 'name': name, 'type': ptype, 'assign': assign, 'default': default}
    
    if op == 'READ_BODY':
        # (READ_BODY Product → body)
        schema = expr[1]
        assign = expr[3] if len(expr) > 3 and expr[2] == '→' else expr[2]
        return {'op': 'READ_BODY', 'schema': schema, 'assign': assign}
    
    if op == 'RESPOND':
        # (RESPOND 200 product) or (RESPOND 201 product) or (RESPOND 204)
        status = expr[1]
        body = expr[2] if len(expr) > 2 else None
        result = {'op': 'RESPOND', 'status': status}
        if body:
            result['body'] = body
        return result
    
    # ── Errors ──
    if op == 'ERR_NOT_FOUND':
        # (ERR_NOT_FOUND "product" id)
        return {'op': 'ERR_NOT_FOUND', 'entity': expr[1], 'id': expr[2]}
    
    if op == 'ERR_INVALID_BODY':
        # (ERR_INVALID_BODY "Product")
        return {'op': 'ERR_INVALID_BODY', 'schema': expr[1]}
    
    if op == 'ERR_BUSINESS_RULE':
        # (ERR_BUSINESS_RULE "rule" "detail")
        return {'op': 'ERR_BUSINESS_RULE', 'rule': expr[1], 'detail': expr[2]}
    
    if op == 'ERR_UNAUTHORIZED':
        return {'op': 'ERR_UNAUTHORIZED'}

    if op == 'ERR_UNAVAILABLE':
        # (ERR_UNAVAILABLE "service_name")
        return {'op': 'ERR_UNAVAILABLE', 'service': expr[1] if len(expr) > 1 else ''}

    if op == 'ERR_TIMEOUT':
        return {'op': 'ERR_TIMEOUT'}

    if op == 'ERR_VALIDATION':
        # (ERR_VALIDATION "field" "reason")
        return {
            'op': 'ERR_VALIDATION',
            'field': expr[1] if len(expr) > 1 else '',
            'reason': expr[2] if len(expr) > 2 else '',
        }

    if op == 'ERR_INVALID_PARAM':
        return {
            'op': 'ERR_INVALID_PARAM',
            'name': expr[1] if len(expr) > 1 else '',
            'expected': expr[2] if len(expr) > 2 else '',
        }
    
    if op == 'ERR_CUSTOM':
        # (ERR_CUSTOM ERR_TYPE (param_name value) (param_name value) ...)
        err_type = expr[1] if len(expr) > 1 else 'ERR_UNKNOWN'
        params = {}
        for item in expr[2:]:
            if isinstance(item, list) and len(item) >= 2:
                params[item[0]] = item[1]
        return {'op': 'ERR_CUSTOM', 'type': err_type, 'params': params}
    
    # ── Events ──
    if op == 'EMIT_EVENT':
        # (EMIT_EVENT product_created product)
        return {'op': 'EMIT_EVENT', 'event': expr[1], 'data': expr[2] if len(expr) > 2 else ''}
    
    # ── Logging ──
    if op == 'LOG':
        # (LOG INFO "message")
        return {'op': 'LOG', 'level': expr[1], 'message': expr[2] if len(expr) > 2 else ''}
    
    # ── Control flow ──
    if op == 'MATCH':
        # (MATCH value (OK ...) (NONE ...) (ERR ...))
        value = expr[1]
        cases = {}
        for case_expr in expr[2:]:
            if isinstance(case_expr, list) and len(case_expr) > 0:
                case_name = case_expr[0]
                case_instructions = extract_instructions(['do'] + case_expr[1:])
                cases[case_name] = case_instructions
        return {'op': 'MATCH', 'value': value, 'cases': cases}
    
    if op == 'BRANCH':
        # (BRANCH (> x y) (TRUE ...) (FALSE ...))
        condition = expr[1]
        cases = {}
        for case_expr in expr[2:]:
            if isinstance(case_expr, list) and len(case_expr) > 0:
                case_name = case_expr[0]
                case_instructions = extract_instructions(['do'] + case_expr[1:])
                cases[case_name] = case_instructions
        
        # Convert S-expr condition to dict
        if isinstance(condition, list):
            cond_dict = {
                'left': condition[1] if len(condition) > 1 else '',
                'op': condition[0],
                'right': condition[2] if len(condition) > 2 else ''
            }
        else:
            cond_dict = str(condition)
        
        return {'op': 'BRANCH', 'condition': cond_dict, 'cases': cases}
    
    # ── Fallback: return as generic instruction ──
    return {'op': op, '_raw': expr}


# ── Test the parser ──

if __name__ == '__main__':
    test_source = '''
    ;; Test parsing
    (service product_service
      (version "0.7")
      (port 8080))
    
    (schema Product
      (field id uuid (auto_gen))
      (field name string (required) (min 1) (max 200)))
    
    (route GET "/products" handler_list)
    
    (handler handler_list
      (auth PUBLIC)
      (do
        (STORE_LIST_ALL product_store → products)
        (RESPOND 200 products)))
    '''
    
    exprs = parse(test_source)
    
    print("Parsed expressions:")
    for expr in exprs:
        print(f"  {expr}")
    
    print()
    
    service = find_form(exprs, 'service')
    print(f"Service: {service}")
    print(f"  Name: {service[1]}")
    print(f"  Port: {find_prop_value(service, 'port')}")
    
    schema = find_form(exprs, 'schema')
    print(f"Schema: {schema[1]}")
    
    routes = find_all_forms(exprs, 'route')
    print(f"Routes: {len(routes)}")
    for r in routes:
        print(f"  {r[1]} {r[2]} → {r[3]}")
    
    handler = find_form(exprs, 'handler')
    print(f"Handler: {handler[1]}")
    do_block = find_prop(handler, 'do')
    instructions = extract_instructions(do_block)
    print(f"  Instructions: {instructions}")
