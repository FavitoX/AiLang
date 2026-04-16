"""
AILang S-expression Bytecode Parser

Reads a .sexpr file and provides the same interface as the JSON BytecodeParser,
so the C# and Go generators work without changes.
"""

from sexpr_parser import (
    parse, find_form, find_all_forms, find_prop, find_prop_value,
    find_all_props, extract_instructions
)


def _dict_from_props(form) -> dict:
    """Collapse `(form (k v) (k v) ...)` nested pairs into a flat dict.

    For `(k v)` pairs with scalar values, stores `{k: v}`. For pairs that
    themselves have nested sub-forms, stores the raw sub-list so callers can
    drill down further.
    """
    out = {}
    if not isinstance(form, list):
        return out
    for item in form[1:]:
        if not (isinstance(item, list) and item):
            continue
        key = str(item[0])
        if len(item) == 2:
            out[key] = item[1]
        elif len(item) > 2:
            out[key] = item[1:]
    return out


class SExprBytecodeParser:
    """Parses AILang .sexpr bytecode and provides a uniform interface for code generators."""

    def __init__(self, filepath: str):
        with open(filepath, 'r') as f:
            source = f.read()
        
        self.exprs = parse(source)
        self._parse_all()
        self.validate()

    def _parse_all(self):
        """Parse all top-level forms into structured data."""
        self._service = self._parse_service()
        self._schemas = self._parse_schemas()
        self._stores = self._parse_stores()
        self._config = self._parse_config()
        self._auth = self._parse_auth()
        self._cors = self._parse_cors()
        self._routes = self._parse_routes()
        self._handlers = self._parse_handlers()
        self._tests = self._parse_tests()
        self._custom_errors = self._parse_custom_errors()
        self._dependencies = self._parse_dependencies()
        self._resilience = self._parse_resilience()
        self._rate_limit = self._parse_rate_limit()
        self._events = self._parse_events()

    def validate(self):
        """Validate the bytecode."""
        if not self._service:
            raise ValueError("Missing (service ...) definition")
        if not self._schemas:
            raise ValueError("Missing (schema ...) definitions")
        if not self._routes:
            raise ValueError("Missing (route ...) definitions")
        if not self._handlers:
            raise ValueError("Missing (handler ...) definitions")
        
        # Validate all handlers have auth
        for name, handler in self._handlers.items():
            if 'auth' not in handler:
                raise ValueError(f"Handler '{name}' missing auth. AILang requires auth on every handler.")
        
        # Validate routes reference existing handlers
        for route in self._routes:
            if route['handler'] not in self._handlers:
                raise ValueError(f"Route references unknown handler: {route['handler']}")

    # ── Parsers for each section ──

    def _parse_service(self) -> dict:
        form = find_form(self.exprs, 'service')
        if not form:
            return {}
        return {
            'name': form[1],
            'port': find_prop_value(form, 'port', 8080),
            'on_fiber_crash': find_prop_value(form, 'on_fiber_crash', 'RESTART_FIBER'),
            'on_service_crash': find_prop_value(form, 'on_service_crash', 'NOTIFY_RESTART'),
        }

    def _parse_schemas(self) -> dict:
        schemas = {}
        for form in find_all_forms(self.exprs, 'schema'):
            name = form[1]
            fields = {}
            for field_form in find_all_props(form, 'field'):
                # (field name type (constraint) (constraint) ...)
                field_name = field_form[1]
                field_type = field_form[2]
                constraints = {}
                
                for c in field_form[3:]:
                    if isinstance(c, list):
                        if c[0] == 'auto_gen':
                            constraints['auto_gen'] = True
                        elif c[0] == 'required':
                            constraints['required'] = True
                        elif c[0] in ('min', 'max', 'gt', 'gte', 'lt', 'lte', 'min_items'):
                            constraints[c[0]] = c[1]
                    elif c == 'required':
                        constraints['required'] = True
                
                fields[field_name] = {'type': field_type, **constraints}
            
            # Parse validations (cross-field)
            validations = []
            for v in find_all_props(form, 'validate'):
                validations.append(v[1:])
            
            schemas[name] = {'fields': fields}
            if validations:
                schemas[name]['validations'] = validations
        
        return schemas

    def _parse_stores(self) -> dict:
        stores = {}
        for form in find_all_forms(self.exprs, 'store'):
            name = form[1]
            provider = find_prop_value(form, 'provider', None)
            legacy_type = find_prop_value(form, 'type', 'IN_MEMORY')
            resolved_provider = provider or legacy_type

            connection = find_prop_value(form, 'connection', '')
            schema = find_prop_value(form, 'schema', '')
            table = find_prop_value(form, 'table', '')

            pool = self._parse_pool(find_prop(form, 'pool'))
            mapping = self._parse_mapping(find_prop(form, 'mapping'))

            stores[name] = {
                'provider': resolved_provider,
                'type': resolved_provider,
                'schema': schema,
                'table': table,
                'connection': connection,
                'pool': pool,
                'mapping': mapping,
                'index': self._parse_index(find_prop(form, 'index')),
            }
        return stores

    def _parse_pool(self, pool_form) -> dict:
        if not pool_form:
            return None
        pool = {
            'min': find_prop_value(pool_form, 'min', 5),
            'max': find_prop_value(pool_form, 'max', 50),
        }
        idle = find_prop(pool_form, 'idle_timeout')
        if idle and len(idle) > 1 and isinstance(idle[1], list) and idle[1][0] == 'duration':
            value = idle[1][1] if len(idle[1]) > 1 else 30
            unit = idle[1][2] if len(idle[1]) > 2 else 'seconds'
            pool['idle_timeout_seconds'] = self._to_seconds(value, unit)
        return pool

    @staticmethod
    def _to_seconds(value, unit) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = 30
        unit = str(unit).lower()
        if unit.startswith('ms') or unit.startswith('millisec'):
            return max(1, v // 1000)
        if unit.startswith('min'):
            return v * 60
        if unit.startswith('hour'):
            return v * 3600
        return v  # seconds by default

    def _parse_mapping(self, mapping_form) -> dict:
        if not mapping_form:
            return {}
        mapping = {}
        for item in mapping_form[1:]:
            if isinstance(item, list) and len(item) >= 2 and item[0] == 'field':
                entry = self._parse_mapping_field(item)
                if entry:
                    mapping[entry['field']] = entry
        return mapping

    @staticmethod
    def _parse_mapping_field(field_form: list) -> dict:
        """Parse (field name → column "col" type "TYPE" [not_null] [unique] [default "..."])."""
        if len(field_form) < 2:
            return None
        entry = {
            'field': field_form[1],
            'column': None,
            'sql_type': None,
            'not_null': False,
            'unique': False,
            'default': None,
        }
        try:
            arrow_idx = field_form.index('→')
        except ValueError:
            arrow_idx = 1  # fall through to scanning everything
        tokens = field_form[arrow_idx + 1:]
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == 'column' and i + 1 < len(tokens):
                entry['column'] = str(tokens[i + 1])
                i += 2
            elif tok == 'type' and i + 1 < len(tokens):
                entry['sql_type'] = str(tokens[i + 1])
                i += 2
            elif tok == 'default' and i + 1 < len(tokens):
                entry['default'] = str(tokens[i + 1])
                i += 2
            elif tok == 'not_null':
                entry['not_null'] = True
                i += 1
            elif tok == 'unique':
                entry['unique'] = True
                i += 1
            else:
                i += 1
        return entry

    def _parse_index(self, index_form) -> dict:
        if not index_form or len(index_form) < 3:
            return {'field': 'id', 'type': 'PRIMARY'}
        return {'field': index_form[1], 'type': index_form[2]}

    def _parse_config(self) -> dict:
        form = find_form(self.exprs, 'config')
        if not form:
            return {}
        config = {}
        for item in form[1:]:
            if isinstance(item, list) and len(item) >= 3:
                if item[1] == 'secret':
                    config[item[0]] = {'type': 'secret', 'ref': item[2]}
                elif item[1] == 'enum':
                    config[item[0]] = {'type': 'enum', 'values': item[2], 'default': item[3] if len(item) > 3 else None}
                else:
                    config[item[0]] = {'type': item[1], 'default': item[2]}
        return config

    def _parse_auth(self) -> dict:
        form = find_form(self.exprs, 'auth')
        if not form:
            return {}
        return {
            'provider': find_prop_value(form, 'provider', 'jwt'),
            'secret_ref': find_prop_value(form, 'secret_ref', 'jwt_secret'),
        }

    def _parse_cors(self) -> dict:
        form = find_form(self.exprs, 'cors')
        if not form:
            return {}
        
        origins = find_prop_value(form, 'origins', '*')
        if isinstance(origins, str):
            origins = [origins]
        
        methods_prop = find_prop(form, 'methods')
        methods = list(methods_prop[1:]) if methods_prop else ['GET']
        
        headers_prop = find_prop(form, 'headers')
        headers = list(headers_prop[1:]) if headers_prop else ['Content-Type']
        
        return {
            'origins': origins,
            'methods': [str(m) for m in methods],
            'headers': [str(h) for h in headers],
            'max_age': find_prop_value(form, 'max_age', 3600),
        }

    def _parse_routes(self) -> list:
        routes = []
        for form in find_all_forms(self.exprs, 'route'):
            routes.append({
                'method': str(form[1]),
                'path': str(form[2]),
                'handler': str(form[3]),
            })
        return routes

    def _parse_handlers(self) -> dict:
        handlers = {}
        for form in find_all_forms(self.exprs, 'handler'):
            name = form[1]
            
            # Parse auth
            auth_prop = find_prop(form, 'auth')
            if auth_prop:
                auth_val = auth_prop[1]
                if auth_val == 'PUBLIC':
                    auth = 'PUBLIC'
                elif isinstance(auth_val, list) and auth_val[0] == 'requires':
                    auth = {'requires': auth_val[1]}
                else:
                    auth = auth_val
            else:
                auth = None
            
            # Parse params
            params = {}
            params_prop = find_prop(form, 'params')
            if params_prop:
                for p in params_prop[1:]:
                    if isinstance(p, list) and len(p) >= 2:
                        params[p[0]] = p[1]
            
            # Parse returns: (returns (RESULT (List ProductStats) Error)) → {wrapper, type}
            returns_prop = find_prop(form, 'returns')
            returns = self._parse_returns_type(returns_prop)
            
            # Parse instructions from (do ...)
            do_block = find_prop(form, 'do')
            instructions = extract_instructions(do_block) if do_block else []
            
            # Event-handler metadata: `(type EVENT_HANDLER)` + `(event NAME)`.
            handler_type = find_prop_value(form, 'type', 'HTTP')
            event_name = find_prop_value(form, 'event', None)

            handlers[name] = {
                'auth': auth,
                'params': params,
                'returns': returns,
                'instructions': instructions,
                'type': str(handler_type),
                'event': str(event_name) if event_name else None,
            }

        return handlers

    @staticmethod
    def _parse_returns_type(returns_prop) -> dict:
        """Parse (returns (RESULT (List X) Error)) into {'wrapper': 'list'|'paginated'|'single'|'void', 'type': 'X'}."""
        if not returns_prop or len(returns_prop) < 2:
            return {'wrapper': 'void', 'type': None}
        inner = returns_prop[1]
        if isinstance(inner, list) and inner and inner[0] == 'RESULT' and len(inner) > 1:
            inner = inner[1]
        if isinstance(inner, list) and len(inner) > 1:
            tag = inner[0]
            if tag == 'List':
                return {'wrapper': 'list', 'type': inner[1]}
            if tag == 'Paginated':
                return {'wrapper': 'paginated', 'type': inner[1]}
            return {'wrapper': 'single', 'type': str(tag)}
        if inner == 'Void' or inner == 'void':
            return {'wrapper': 'void', 'type': None}
        return {'wrapper': 'single', 'type': str(inner)}

    def _parse_tests(self) -> list:
        tests_form = find_form(self.exprs, 'tests')
        if not tests_form:
            return []
        
        tests = []
        for test_form in find_all_props(tests_form, 'test'):
            test = {'name': test_form[1]}
            
            setup = find_prop(test_form, 'setup')
            run = find_prop(test_form, 'run')
            clean = find_prop(test_form, 'clean')
            
            if setup:
                test['setup'] = setup[1:]
            if run:
                test['run'] = run[1:]
            if clean:
                test['clean'] = clean[1:]
            
            tests.append(test)
        
        return tests

    def _parse_custom_errors(self) -> dict:
        """Parse custom errors from either:
          - (custom_errors (ERR_X (http_status N) (params a b c) (message_template "...")) ...)
          - (define_error ERR_X N)  — legacy shortform
        """
        errors = {}

        # New format: a single (custom_errors ...) top-level form.
        form = find_form(self.exprs, 'custom_errors')
        if form:
            for item in form[1:]:
                if not (isinstance(item, list) and item):
                    continue
                name = item[0]
                entry = {'http_status': 422, 'params': [], 'message_template': ''}
                for prop in item[1:]:
                    if not (isinstance(prop, list) and prop):
                        continue
                    if prop[0] == 'http_status' and len(prop) > 1:
                        try:
                            entry['http_status'] = int(prop[1])
                        except (TypeError, ValueError):
                            entry['http_status'] = 422
                    elif prop[0] == 'params':
                        entry['params'] = [str(p) for p in prop[1:]]
                    elif prop[0] == 'message_template' and len(prop) > 1:
                        entry['message_template'] = str(prop[1])
                errors[name] = entry

        # Legacy shortform — preserve if present, filling defaults.
        for legacy in find_all_forms(self.exprs, 'define_error'):
            name = legacy[1]
            errors.setdefault(name, {'http_status': 422, 'params': [], 'message_template': ''})
            try:
                errors[name]['http_status'] = int(legacy[2])
            except (IndexError, TypeError, ValueError):
                pass
        return errors

    def _parse_dependencies(self) -> dict:
        """Parse dependency declarations. Two shapes supported:
          - legacy: (depends_on name1 name2 ...) — flat references, treated as STORE
          - rich: (dependencies (name (type X) (target T) (base_url_ref ENV.Y) (default "...") (resilience policy)))
        """
        deps = {}

        # Legacy form.
        for form in find_all_forms(self.exprs, 'depends_on'):
            for dep in form[1:]:
                deps[str(dep)] = {'type': 'STORE', 'target': str(dep)}

        # Rich form.
        form = find_form(self.exprs, 'dependencies')
        if form:
            for item in form[1:]:
                if not (isinstance(item, list) and item):
                    continue
                name = str(item[0])
                # Short form: (name target) → plain reference
                if len(item) == 2 and not isinstance(item[1], list):
                    deps[name] = {'type': 'STORE', 'target': str(item[1])}
                    continue
                entry = {'type': 'STORE'}
                for prop in item[1:]:
                    if not (isinstance(prop, list) and prop):
                        continue
                    key = prop[0]
                    if key in ('type', 'target', 'base_url_ref', 'default', 'resilience') and len(prop) > 1:
                        entry[key] = str(prop[1])
                deps[name] = entry
        return deps

    def service_refs(self) -> dict:
        """Convenience: subset of dependencies whose type == SERVICE_REF."""
        return {n: d for n, d in self._dependencies.items() if d.get('type') == 'SERVICE_REF'}

    def _parse_resilience(self) -> dict:
        """Parse (resilience (policies (POLICY_NAME (timeout ...) (circuit_breaker ...) (retry ...))))."""
        form = find_form(self.exprs, 'resilience')
        if not form:
            return {'policies': {}}

        policies = {}
        policies_form = None
        for item in form[1:]:
            if isinstance(item, list) and item and item[0] == 'policies':
                policies_form = item
                break
        if not policies_form:
            return {'_raw': form, 'policies': {}}

        for policy in policies_form[1:]:
            if not (isinstance(policy, list) and policy):
                continue
            name = str(policy[0])
            entry = {'name': name, 'timeout': None, 'circuit_breaker': None, 'retry': None}
            for sub in policy[1:]:
                if not (isinstance(sub, list) and sub):
                    continue
                key = sub[0]
                if key == 'timeout':
                    entry['timeout'] = {
                        'duration_ms': _dict_from_props(sub).get('duration_ms', 3000),
                        'on_timeout': _dict_from_props(sub).get('on_timeout', 'ERR_TIMEOUT'),
                    }
                elif key == 'circuit_breaker':
                    props = _dict_from_props(sub)
                    entry['circuit_breaker'] = {
                        'failure_threshold': int(props.get('failure_threshold', 5)),
                        'success_threshold': int(props.get('success_threshold', 3)),
                        'half_open_max_calls': int(props.get('half_open_max_calls', 1)),
                        'reset_timeout_ms': int(props.get('reset_timeout_ms', 30000)),
                    }
                elif key == 'retry':
                    props = _dict_from_props(sub)
                    backoff = props.get('backoff')
                    if isinstance(backoff, list):
                        backoff = _dict_from_props(['backoff'] + backoff)
                    elif not isinstance(backoff, dict):
                        backoff = {}
                    retryable = []
                    non_retryable = []
                    for s in sub[1:]:
                        if isinstance(s, list) and s:
                            if s[0] == 'retryable_errors':
                                retryable = [str(x) for x in s[1:]]
                            elif s[0] == 'non_retryable_errors':
                                non_retryable = [str(x) for x in s[1:]]
                    entry['retry'] = {
                        'max_attempts': int(props.get('max_attempts', 3)),
                        'backoff_type': backoff.get('type', 'exponential'),
                        'initial_ms': int(backoff.get('initial_ms', 100)),
                        'max_ms': int(backoff.get('max_ms', 2000)),
                        'retryable_errors': retryable,
                        'non_retryable_errors': non_retryable,
                    }
            policies[name] = entry
        return {'policies': policies, '_raw': form}

    def _parse_events(self) -> dict:
        """Parse (events (definitions (name (schema X) (description "...")) ...)
                         (subscriptions (item (event X) (handler Y)) ...))."""
        form = find_form(self.exprs, 'events')
        if not form:
            return {'definitions': {}, 'subscriptions': []}

        definitions = {}
        subscriptions = []
        for item in form[1:]:
            if not (isinstance(item, list) and item):
                continue
            if item[0] == 'definitions':
                for evt in item[1:]:
                    if isinstance(evt, list) and evt:
                        name = str(evt[0])
                        props = _dict_from_props(evt)
                        definitions[name] = {
                            'schema': str(props.get('schema', '')),
                            'description': str(props.get('description', '')),
                        }
            elif item[0] == 'subscriptions':
                for sub in item[1:]:
                    if isinstance(sub, list) and sub:
                        props = _dict_from_props(sub)
                        if 'event' in props and 'handler' in props:
                            subscriptions.append({
                                'event': str(props['event']),
                                'handler': str(props['handler']),
                            })
        return {'definitions': definitions, 'subscriptions': subscriptions}

    def _parse_rate_limit(self) -> dict:
        form = find_form(self.exprs, 'rate_limit')
        if not form:
            return {}
        return {
            'max_rps': find_prop_value(form, 'max_rps', 0),
            'burst': find_prop_value(form, 'burst', 0),
            'on_limit': find_prop_value(form, 'on_limit', 'ERR_RATE_LIMITED'),
        }

    # ── Public interface (same as JSON BytecodeParser) ──

    @property
    def raw(self):
        return {
            'version': find_prop_value(find_form(self.exprs, 'service'), 'version', '0.7'),
            'service': self._service,
            'schemas': self._schemas,
            'stores': self._stores,
            'routes': self._routes,
            'handlers': self._handlers,
            'tests': self._tests,
        }

    @property
    def service(self): return self._service

    @property
    def schemas(self): return self._schemas

    @property
    def stores(self): return self._stores

    @property
    def config(self): return self._config

    @property
    def cors(self): return self._cors

    @property
    def auth(self): return self._auth

    @property
    def routes(self): return self._routes

    @property
    def handlers(self): return self._handlers

    @property
    def custom_errors(self): return self._custom_errors

    @property
    def tests(self): return self._tests

    @property
    def dependencies(self): return self._dependencies

    @property
    def resilience(self): return self._resilience

    @property
    def events(self): return self._events


# ── Test ──

if __name__ == '__main__':
    import sys
    
    filepath = sys.argv[1] if len(sys.argv) > 1 else None
    if not filepath:
        print("Usage: python sexpr_bytecode_parser.py <file.sexpr>")
        sys.exit(1)
    
    bc = SExprBytecodeParser(filepath)
    
    print(f"Service: {bc.service['name']}")
    print(f"Port: {bc.service['port']}")
    print(f"Schemas: {list(bc.schemas.keys())}")
    print(f"Stores: {list(bc.stores.keys())}")
    print(f"Routes: {len(bc.routes)}")
    for r in bc.routes:
        print(f"  {r['method']} {r['path']} → {r['handler']}")
    print(f"Handlers: {list(bc.handlers.keys())}")
    for name, h in bc.handlers.items():
        print(f"  {name}: auth={h['auth']}, instructions={len(h['instructions'])}")
    print(f"Tests: {len(bc.tests)}")
    print(f"CORS: {bc.cors}")
    print(f"Auth: {bc.auth}")
