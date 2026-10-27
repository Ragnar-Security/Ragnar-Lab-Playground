"""The shared `app` fixture must give tests a real, per-test database.

An in-memory SQLite engine is one connection shared by every thread, so a
background thread returning it to the pool rolls back whatever the request
thread has not committed yet. Production pools never share connections.
"""

import os
import tempfile
import threading

from app.models import db


def test_app_fixture_uses_its_per_test_sqlite_file(app):
    with app.app_context():
        database = db.engine.url.database

    assert database not in (None, '', ':memory:')
    assert os.path.isfile(database)
    assert os.path.dirname(database) == tempfile.gettempdir()


def test_app_fixture_gives_concurrent_threads_their_own_connections(app):
    both_connected = threading.Barrier(2, timeout=5)
    connections = {}

    def hold_connection(name):
        with app.app_context(), db.engine.connect() as connection:
            connections[name] = connection.connection.dbapi_connection
            both_connected.wait()

    threads = [threading.Thread(target=hold_connection, args=(name,))
               for name in ('first', 'second')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert connections['first'] is not connections['second']
