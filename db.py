"""Eventora – MySQL access layer (PyMySQL).

Every query in the project lives here so that app.py stays about HTTP and the
templates stay about markup. All MySQL errors are re-raised as
``DatabaseUnavailable`` so the website can show a friendly message instead of a
stack trace when the database is not running yet.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from config import Config


class DatabaseUnavailable(RuntimeError):
    """MySQL could not be reached, or rejected the query."""


# --------------------------------------------------------------------------- #
#  Connection handling
# --------------------------------------------------------------------------- #
def _connect(with_database: bool = True):
    """Opens one connection. `with_database=False` is used to CREATE DATABASE."""
    kwargs = {
        'host': Config.MYSQL_HOST,
        'port': Config.MYSQL_PORT,
        'user': Config.MYSQL_USER,
        'password': Config.MYSQL_PASSWORD,
        'charset': Config.MYSQL_CHARSET,
        'cursorclass': DictCursor,
        'autocommit': False,
        'connect_timeout': Config.MYSQL_CONNECT_TIMEOUT,
    }
    if with_database:
        kwargs['database'] = Config.MYSQL_DATABASE
    try:
        return pymysql.connect(**kwargs)
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable(
            f'Cannot connect to MySQL at {Config.MYSQL_HOST}:{Config.MYSQL_PORT} '
            f'as "{Config.MYSQL_USER}" ({exc}).'
        ) from exc


@contextmanager
def connection(with_database: bool = True):
    """Yields a connection and commits (or rolls back) automatically."""
    conn = _connect(with_database)
    try:
        yield conn
        conn.commit()
    except pymysql.MySQLError as exc:
        conn.rollback()
        raise DatabaseUnavailable(str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run(sql: str, params=None, fetch: str | None = None):
    """Runs a single statement.

    fetch='all'  -> list of rows       fetch='one' -> one row or None
    fetch=None   -> last insert id for an INSERT, otherwise the affected rows
    """
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == 'all':
                    return cur.fetchall()
                if fetch == 'one':
                    return cur.fetchone()
                return cur.lastrowid or cur.rowcount
    except DatabaseUnavailable:
        raise
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable(str(exc)) from exc


def fetch_all(sql: str, params=None) -> list[dict]:
    return _run(sql, params, fetch='all') or []


def fetch_one(sql: str, params=None) -> dict | None:
    return _run(sql, params, fetch='one')


def execute(sql: str, params=None) -> int:
    return int(_run(sql, params) or 0)


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #
_COMMENT_RE = re.compile(r'^\s*--.*$', re.MULTILINE)


def split_statements(script: str) -> list[str]:
    """Splits a .sql file into single statements (comments removed)."""
    cleaned = _COMMENT_RE.sub('', script)
    return [s.strip() for s in cleaned.split(';') if s.strip()]


def ensure_schema() -> dict:
    """Creates the database, the tables and the indexes. Safe to run again."""
    if not Config.SCHEMA_FILE.is_file():
        raise DatabaseUnavailable(f'Schema file not found: {Config.SCHEMA_FILE}')

    statements = split_statements(Config.SCHEMA_FILE.read_text(encoding='utf-8'))
    applied, skipped = 0, 0

    # NOTE: the old code skipped CREATE DATABASE / USE while connected with
    # with_database=False, so CREATE TABLE ran with "no database selected"
    # and init-db always failed. Fixed: create the DB explicitly, select it,
    # then apply every other statement.
    db_name = Config.MYSQL_DATABASE
    # Very small identifier guard – database name comes from .env.
    if not re.fullmatch(r'[A-Za-z0-9_]+', db_name or ''):
        raise DatabaseUnavailable(f'Invalid MYSQL_DATABASE name: {db_name!r}')

    try:
        with connection(with_database=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'CREATE DATABASE IF NOT EXISTS `{db_name}` '
                    'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci'
                )
                conn.select_db(db_name)
                for statement in statements:
                    upper = statement.upper().lstrip()
                    if upper.startswith('CREATE DATAB') or upper.startswith('USE '):
                        skipped += 1
                        continue
                    cur.execute(statement)
                    applied += 1
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable(str(exc)) from exc
    return {'statements': applied, 'skipped': skipped,
            'database': Config.MYSQL_DATABASE}


def server_version() -> str:
    row = fetch_one('SELECT VERSION() AS version')
    return (row or {}).get('version', 'unknown')


# --------------------------------------------------------------------------- #
#  Administrators
# --------------------------------------------------------------------------- #
def get_admin_by_id(admin_id: int) -> dict | None:
    return fetch_one(
        'SELECT id, username, display_name, password_hash, created_at, last_login_at '
        'FROM admins WHERE id = %s',
        (admin_id,),
    )


def get_admin_by_username(username: str) -> dict | None:
    return fetch_one(
        'SELECT id, username, display_name, password_hash, created_at, last_login_at '
        'FROM admins WHERE username = %s',
        (username,),
    )


def count_admins() -> int:
    row = fetch_one('SELECT COUNT(*) AS total FROM admins')
    return int((row or {}).get('total') or 0)


def upsert_admin(username: str, password_hash: str, display_name: str | None = None) -> int:
    """Creates the account, or resets the password of an existing one."""
    return execute(
        'INSERT INTO admins (username, password_hash, display_name) '
        'VALUES (%s, %s, %s) '
        'ON DUPLICATE KEY UPDATE password_hash = VALUES(password_hash), '
        '                        display_name = VALUES(display_name)',
        (username, password_hash, display_name),
    )


def touch_admin_login(admin_id: int) -> None:
    execute('UPDATE admins SET last_login_at = NOW() WHERE id = %s', (admin_id,))


# --------------------------------------------------------------------------- #
#  Media library
# --------------------------------------------------------------------------- #
# Only these keys can ever be written from a form or a JSON body.
MUTABLE_FIELDS = ('title', 'category', 'kind', 'file_name', 'poster_name',
                  'mime_type', 'file_size', 'is_private', 'admin_id')


def list_media(visibility: str = 'all', category: str | None = None,
               kind: str | None = None, limit: int = 200) -> list[dict]:
    """Media rows, newest first.

    visibility: 'all'
                'published'  -> is_private = 0, the only rows viewers may see
                'private'    -> is_private = 1, admin-only material
    """
    sql = [
        'SELECT m.id, m.title, m.category, m.kind, m.file_name, m.poster_name,',
        '       m.mime_type, m.file_size, m.is_private, m.admin_id,',
        '       m.created_at, m.updated_at, a.username AS uploaded_by',
        'FROM media m LEFT JOIN admins a ON a.id = m.admin_id',
        'WHERE 1 = 1',
    ]
    params: list = []

    if visibility == 'published':
        sql.append('AND m.is_private = 0')
    elif visibility == 'private':
        sql.append('AND m.is_private = 1')
    if category:
        sql.append('AND m.category = %s')
        params.append(category)
    if kind:
        sql.append('AND m.kind = %s')
        params.append(kind)

    sql.append('ORDER BY m.created_at DESC, m.id DESC LIMIT %s')
    params.append(int(limit))
    return fetch_all(' '.join(sql), params)


def get_media(media_id: int) -> dict | None:
    return fetch_one(
        'SELECT m.*, a.username AS uploaded_by FROM media m '
        'LEFT JOIN admins a ON a.id = m.admin_id WHERE m.id = %s',
        (media_id,),
    )


def find_media_by_file(file_name: str) -> dict | None:
    """Used by /media/<file> to decide whether a file may leave the server.

    Matches the file itself or the poster of a film. A published row wins over a
    private one, so a public file can never be hidden by a private duplicate.
    """
    return fetch_one(
        'SELECT id, kind, file_name, poster_name, is_private, title, category '
        'FROM media WHERE file_name = %s OR poster_name = %s '
        'ORDER BY is_private ASC, id ASC LIMIT 1',
        (file_name, file_name),
    )


def create_media(fields: dict) -> int:
    columns = [c for c in MUTABLE_FIELDS if c in fields]
    if not columns:
        raise ValueError('create_media() needs at least one field')
    placeholders = ', '.join(['%s'] * len(columns))
    return execute(
        f"INSERT INTO media ({', '.join(columns)}) VALUES ({placeholders})",
        [fields[c] for c in columns],
    )


def update_media(media_id: int, fields: dict) -> int:
    columns = [c for c in MUTABLE_FIELDS if c in fields]
    if not columns:
        return 0
    assignments = ', '.join(f'{c} = %s' for c in columns)
    return execute(
        f'UPDATE media SET {assignments} WHERE id = %s',
        [fields[c] for c in columns] + [media_id],
    )


def delete_media(media_id: int) -> dict | None:
    """Removes the row and returns it, so the files can be deleted from disk."""
    row = get_media(media_id)
    if row is None:
        return None
    execute('DELETE FROM media WHERE id = %s', (media_id,))
    return row


def media_stats() -> dict:
    """Dashboard counters: total / published / admin-only / images / films."""
    row = fetch_one(
        'SELECT COUNT(*) AS total, '
        '       COALESCE(SUM(is_private = 0), 0) AS published, '
        '       COALESCE(SUM(is_private = 1), 0) AS private_items, '
        "       COALESCE(SUM(kind = 'image'), 0) AS images, "
        "       COALESCE(SUM(kind = 'video'), 0) AS videos "
        'FROM media'
    ) or {}
    return {
        'total': int(row.get('total') or 0),
        'published': int(row.get('published') or 0),
        'private_items': int(row.get('private_items') or 0),
        'images': int(row.get('images') or 0),
        'videos': int(row.get('videos') or 0),
    }
