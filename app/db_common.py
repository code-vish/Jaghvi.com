"""Database values independent of any native driver's cursor implementation."""
from __future__ import annotations
import sqlite3


class DatabaseError(RuntimeError):
    def __init__(self, message='The database is unavailable.', *, code='DB_UNAVAILABLE'):
        super().__init__(message)
        self.code = code


class IntegrityError(DatabaseError):
    def __init__(self, message='A database constraint prevented this change.', *, code='DB_CONSTRAINT'):
        super().__init__(message, code=code)


class Row:
    """Support row[0], row['name'], iteration and dict(row)."""
    __slots__ = ('_columns', '_values', '_index')

    def __init__(self, columns, values):
        if len(columns) != len(values):
            raise DatabaseError('Inconsistent database column metadata.', code='DB_PROTOCOL')
        self._columns, self._values = tuple(columns), tuple(values)
        self._index = {}
        for i, name in enumerate(columns):
            self._index.setdefault(name, i)

    def __getitem__(self, key):
        return self._values[self._index[key]] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._columns)


class ResultCursor:
    """Materialized HTTP result: no native cursor methods or thread affinity."""
    def __init__(self, columns=(), values=(), *, rowcount=-1, lastrowid=None):
        self.description = [(c, None, None, None, None, None, None) for c in columns]
        self.rowcount, self.lastrowid = rowcount, lastrowid
        self._rows = [r if isinstance(r, (Row, sqlite3.Row)) else Row(columns, r) for r in values]
        self._position = 0

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class CursorAdapter:
    """Standard-library SQLite only. Turso does not use this adapter."""
    def __init__(self, cursor):
        self._cursor = cursor
        self._columns = [d[0] for d in (cursor.description or [])]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description

    def _row(self, value):
        if value is None or isinstance(value, (sqlite3.Row, Row)):
            return value
        return Row(self._columns, value)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


def split_sql_script(script: str) -> list[str]:
    """Don't split semicolons inside strings, comments or trigger bodies."""
    statements, buffer = [], ''
    for char in script:
        buffer += char
        if char == ';' and sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ''
    if buffer.strip():
        statements.append(buffer.strip())
    return statements
