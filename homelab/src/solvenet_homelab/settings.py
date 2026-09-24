"""Operator-managed local metadata, independent of coordinator storage."""

import sqlite3
from contextlib import closing, contextmanager
from urllib.parse import urlsplit


DEFAULT_COORDINATOR = 'http://127.0.0.1:8080'
LOCATIONS = ('On this device', 'Local network', 'Cloud API')


def coordinator_url(value):
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError('Invalid coordinator URL') from error
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or '?' in value or '#' in value
            or any(char.isspace() for char in value)):
        raise ValueError('Coordinator URL must be an HTTP(S) origin without credentials, path, query or fragment')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('Invalid coordinator port') from error
    return value.rstrip('/')


class Settings:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if existing - {'settings', 'models', 'sqlite_sequence'}:
                raise ValueError('Site database must be separate from coordinator storage')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    coordinator_url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models (
                    coordinator_url TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    location TEXT NOT NULL,
                    PRIMARY KEY (coordinator_url, model_id)
                );
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (DEFAULT_COORDINATOR,))

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def selected(self):
        with self.connect() as db:
            return db.execute('SELECT coordinator_url FROM settings WHERE id = 1').fetchone()[0]

    def select(self, url):
        url = coordinator_url(url)
        with self.connect() as db:
            db.execute('UPDATE settings SET coordinator_url = ? WHERE id = 1', (url,))

    def add_model(self, model_id, display_name, provider, location):
        if not all(value and value.strip() for value in (model_id, display_name, provider)):
            raise ValueError('Model ID, display name and provider are required')
        if location not in LOCATIONS:
            raise ValueError('Invalid execution location')
        with self.connect() as db:
            db.execute('''INSERT INTO models VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(coordinator_url, model_id) DO UPDATE SET
                display_name = excluded.display_name, provider = excluded.provider,
                location = excluded.location''',
                (self.selected(), model_id, display_name, provider, location))

    def models(self, url):
        with self.connect() as db:
            return db.execute('''SELECT model_id, display_name, provider, location
                FROM models WHERE coordinator_url = ? ORDER BY display_name, model_id''', (url,)).fetchall()
