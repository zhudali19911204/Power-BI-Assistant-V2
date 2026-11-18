"""Join retrieved business definitions to the current model snapshot for one LLM request."""
from collections import defaultdict
from dataclasses import asdict, replace


_IDENTITY = ('kind', 'table', 'name')
_ENDPOINTS = ('from_table', 'from_column', 'to_table', 'to_column')
_UNKNOWN_TYPES = {'', 'auto', 'automatic', 'unknown'}


class ModelPrompt:
    def __init__(self, model, selection, lookups=(), *, include_expressions=True):
        self.model = model
        self.include_expressions = include_expressions
        self.columns = {(table, c.name): c for table, cols in model.tables.items() for c in cols}
        self.measures = {(m.table, m.name): m for m in model.measures}
        self.relations = defaultdict(list)
        for i, relation in enumerate(model.relationships):
            native = asdict(relation)
            self.relations[tuple(native[k] for k in _ENDPOINTS)].append((i, native))
        self.entries = {}
        self.sent_relations = []
        self.covered_relations = set()
        self.dictionary_items = {}
        self.model_status = {}
        self.semantic = self._selection(selection) if selection is not None else None
        self.lookups = [
            {**record, 'context': self._selection(record['context'])}
            if record.get('ok') and record.get('context') is not None else record
            for record in lookups
        ]

    def _entry(self, source):
        kind, table, name = key = tuple(source[k] for k in _IDENTITY)
        entry = {k: v for k, v in source.items() if v is not None and v != '' and v != []}
        entry.pop('in_snapshot', None)
        if kind == 'column' and (column := self.columns.get((table, name))):
            actual = entry.get('actual_dtype', '').strip().casefold()
            native = (column.dtype or '').strip().casefold()
            if native not in _UNKNOWN_TYPES or actual in _UNKNOWN_TYPES:
                if actual not in _UNKNOWN_TYPES and actual != native:
                    entry['semantic_dtype'] = entry['actual_dtype']
                entry['actual_dtype'] = column.dtype
            if column.cardinality is not None:
                entry['cardinality'] = column.cardinality
        elif kind == 'measure' and (measure := self.measures.get((table, name))):
            entry.pop('expression', None)
            if self.include_expressions:
                entry['expression'] = measure.expression or None
        elif kind == 'table' and table == name and self.model.has_table(table):
            if 'date_table' in entry or table in self.model.date_tables:
                entry['date_table'] = table in self.model.date_tables
            if 'calculated_table' in entry or table in self.model.calculated_tables:
                entry['calculated_table'] = table in self.model.calculated_tables
        else:
            entry['in_snapshot'] = False
            entry.pop('expression', None)

        previous = self.entries.get(key)
        self.entries[key] = entry
        if previous is None:
            return entry
        # The identity records a hit for this lookup. Only changed facts need another value.
        delta = {k: v for k, v in entry.items() if k in _IDENTITY or previous.get(k) != v}
        delta.update((k, None) for k in previous if k not in entry)
        return delta

    def _relationships(self, relations):
        added = []
        for rel in relations:
            candidates = self.relations.get(tuple(rel.get(k) for k in _ENDPOINTS), ())
            exact = [(i, r) for i, r in candidates if all(rel.get(k) == v for k, v in r.items())]
            # Multiple native relationships can share endpoints. Preserve their real directions
            # and activation flags even if the retrieved metadata is incomplete or out of date.
            records = []
            for i, native in exact or candidates:
                self.covered_relations.add(i)
                records.append({**rel, **native})
            if not records:
                records.append({**{k: v for k, v in rel.items() if k not in ('cross_filter', 'is_active')},
                                'in_snapshot': False})
            for record in records:
                if record not in self.sent_relations:
                    self.sent_relations.append(record)
                    added.append(record)
        return added

    def _dictionary_item(self, section, item):
        key = (section, item.get('id') or item.get('title'))
        if self.dictionary_items.get(key) == item:
            return {k: item[k] for k in ('id', 'title') if k in item} or {'already_provided': True}
        self.dictionary_items[key] = item
        return item

    def _selection(self, selection):
        data = dict(selection)
        for key in ('status', 'metadata_status', 'pending'):
            if key in self.model_status and self.model_status[key] == data.get(key):
                data.pop(key, None)
            elif key in data:
                self.model_status[key] = data[key]
        retrieval = dict(data.get('retrieval', {}))
        if 'relationships' in retrieval:
            retrieval['relationships'] = self._relationships(retrieval['relationships'])
            data['retrieval'] = retrieval
        if 'business_dictionary' in data:
            dictionary = dict(data['business_dictionary'])
            if dictionary.get('overview'):
                dictionary['overview'] = self._dictionary_item('overview', dictionary['overview'])
            for section in ('terms', 'conventions', 'stale'):
                if section in dictionary:
                    dictionary[section] = [self._dictionary_item(section, item) for item in dictionary[section]]
            data['business_dictionary'] = dictionary
        # Keep rules, business definitions, relationships and non-measure entries in a common
        # prefix for planning and summarization. Only planning needs the native DAX expressions.
        rows = data.pop('entries', [])
        data['entries'] = [self._entry(e) for e in sorted(rows, key=lambda e: e['kind'] == 'measure')]
        return data

    def _remaining_columns(self, table):
        return [c for c in self.model.tables.get(table, ()) if ('column', table, c.name) not in self.entries]

    def query_schema(self, name):
        # The current M script and all referenceable queries are independent of Tabular semantics.
        query_model = replace(self.model, tables={name: self._remaining_columns(name)})
        return query_model.serialize_query_for_prompt(name)

    def schema(self):
        """Return structural facts still needed alongside the semantic records."""
        if not self.entries and not self.covered_relations:
            return self.model.serialize_for_prompt()
        tables = {}
        described_tables = {entry['table'] for entry in self.entries.values() if entry.get('in_snapshot', True)}
        date_tables = [t for t in self.model.date_tables if ('table', t, t) not in self.entries]
        for table in self.model.tables:
            columns = self._remaining_columns(table)
            if columns or table not in described_tables or table in date_tables:
                tables[table] = columns
        remaining = replace(self.model, tables=tables,
                            date_tables=date_tables,
                            calculated_tables={t: v for t, v in self.model.calculated_tables.items()
                                               if ('table', t, t) not in self.entries},
                            relationships=[r for i, r in enumerate(self.model.relationships) if i not in self.covered_relations],
                            measures=[m for m in self.model.measures if ('measure', m.table, m.name) not in self.entries])
        return remaining.serialize_for_prompt(group_columns=True)
