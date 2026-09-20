"""Shared fixtures.

The suite deliberately runs with no database. Every test here either exercises
pure logic or swaps `database`'s connection pool for a fake, so `pytest` works
on a laptop with nothing running and in any CI container.
"""

import pytest

import database


class FakeCursor:
    """Stands in for a psycopg2 cursor. `boom=True` makes execute() raise the
    way a real one does on a SQL error, a statement timeout, or a connection
    the server has dropped."""

    def __init__(self, boom=False):
        self.boom = boom
        self.closed = False

    def execute(self, query, params=None):
        if self.boom:
            raise RuntimeError("simulated query failure")
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, fail_queries):
        self.fail_queries = fail_queries
        self.autocommit = None

    def cursor(self, cursor_factory=None):
        # PooledConnection.__init__ opens a plain cursor to SET search_path;
        # that has to succeed or nothing gets far enough to leak. Real queries
        # go through execute(), which always passes a cursor_factory -- those
        # are the ones we fail.
        return FakeCursor(boom=self.fail_queries and cursor_factory is not None)

    def commit(self):
        pass

    def rollback(self):
        pass


class FakePool:
    """Counts checkouts so a test can assert the pool is whole again.

    putconn() rejects a connection it did not hand out, exactly as psycopg2's
    pool does ("trying to put unkeyed connection"). That matters: returning one
    twice is a real failure, not a harmless double-decrement, and a pool that
    silently tolerated it would let a test report a quiet -1 instead of the
    exception production would raise.
    """

    closed = False

    def __init__(self, fail_queries=False):
        self.fail_queries = fail_queries
        self.checked_out = 0
        self.handed_out = 0
        self._live = set()

    def getconn(self):
        conn = FakeConnection(self.fail_queries)
        self._live.add(id(conn))
        self.checked_out += 1
        self.handed_out += 1
        return conn

    def putconn(self, conn):
        if id(conn) not in self._live:
            raise RuntimeError("trying to put unkeyed connection")
        self._live.discard(id(conn))
        self.checked_out -= 1


@pytest.fixture
def failing_pool(monkeypatch):
    """Install a pool whose every query raises, and hand the test the pool so
    it can check nothing was left checked out."""
    pool = FakePool(fail_queries=True)
    monkeypatch.setattr(database, "_pool", pool)
    monkeypatch.setattr(database, "_get_pool", lambda: pool)
    database.clear_query_cache()
    yield pool
    database.clear_query_cache()


@pytest.fixture
def working_pool(monkeypatch):
    """Same, but queries succeed and return empty results."""
    pool = FakePool(fail_queries=False)
    monkeypatch.setattr(database, "_pool", pool)
    monkeypatch.setattr(database, "_get_pool", lambda: pool)
    database.clear_query_cache()
    yield pool
    database.clear_query_cache()
