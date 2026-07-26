from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Column

from subtitle_sidecar.db.models import Base


def create_sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    if engine.dialect.name == "sqlite":
        _configure_sqlite_connections(engine)
    return engine


def _configure_sqlite_connections(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(connection, _connection_record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            cursor.close()


def create_tables(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        missing_tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name not in existing_tables
        ]
        if missing_tables:
            Base.metadata.create_all(connection, tables=missing_tables)

        inspector = inspect(connection)
        for table in Base.metadata.sorted_tables:
            if table.name not in set(inspector.get_table_names()):
                continue
            existing_columns = {
                column["name"]
                for column in inspector.get_columns(table.name)
            }
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                if not _is_safe_sqlite_additive_column(column):
                    continue
                quoted_table = connection.dialect.identifier_preparer.quote_identifier(table.name)
                _add_sqlite_column(connection, quoted_table, column)
            for column in table.columns:
                if _sqlite_expression_default_sql(column) is None:
                    continue
                _ensure_sqlite_insert_default_trigger(connection, table.name, column.name)


def _is_safe_sqlite_additive_column(column: Column) -> bool:
    if column.primary_key or column.unique:
        return False
    if column.nullable:
        return True
    return _sqlite_default_sql(column) is not None or _sqlite_expression_default_sql(column) is not None


def _add_sqlite_column(connection, quoted_table: str, column: Column) -> None:
    column_sql = _render_sqlite_column_definition(column)
    try:
        connection.exec_driver_sql(f"ALTER TABLE {quoted_table} ADD COLUMN {column_sql}")
    except OperationalError:
        fallback_sql = _render_sqlite_column_definition(column, for_expression_fallback=True)
        if fallback_sql == column_sql:
            raise
        connection.exec_driver_sql(f"ALTER TABLE {quoted_table} ADD COLUMN {fallback_sql}")


def _ensure_sqlite_insert_default_trigger(connection, table_name: str, column_name: str) -> None:
    trigger_name = f"trg_{table_name}_{column_name}_set_default_after_insert"
    quoted_trigger = connection.dialect.identifier_preparer.quote_identifier(trigger_name)
    quoted_table = connection.dialect.identifier_preparer.quote_identifier(table_name)
    quoted_column = connection.dialect.identifier_preparer.quote_identifier(column_name)
    connection.exec_driver_sql(
        f"""
        CREATE TRIGGER IF NOT EXISTS {quoted_trigger}
        AFTER INSERT ON {quoted_table}
        FOR EACH ROW
        WHEN NEW.{quoted_column} IS NULL
        BEGIN
            UPDATE {quoted_table}
            SET {quoted_column} = CURRENT_TIMESTAMP
            WHERE rowid = NEW.rowid;
        END
        """
    )


def _render_sqlite_column_definition(
    column: Column,
    *,
    for_expression_fallback: bool = False,
) -> str:
    parts = [f'"{column.name}"', column.type.compile(dialect=sqlite_dialect())]
    default_sql = _sqlite_default_sql(column)
    expression_default_sql = _sqlite_expression_default_sql(column)
    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
    elif expression_default_sql is not None and not for_expression_fallback:
        parts.append(f"DEFAULT {expression_default_sql}")
    if not column.nullable and not (expression_default_sql is not None and for_expression_fallback):
        parts.append("NOT NULL")
    return " ".join(parts)


def _sqlite_default_sql(column: Column) -> str | None:
    if column.server_default is not None:
        return _sqlite_literal_sql(getattr(column.server_default, "arg", None))
    if column.default is not None:
        return _sqlite_literal_sql(getattr(column.default, "arg", None))
    return None


def _sqlite_expression_default_sql(column: Column) -> str | None:
    if column.server_default is None:
        return None
    expression = getattr(column.server_default, "arg", None)
    if expression is None or isinstance(expression, str):
        return None
    if not hasattr(expression, "compile"):
        return None
    compiled = str(
        expression.compile(
            dialect=sqlite_dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).strip()
    return compiled or None


def _sqlite_literal_sql(value: object) -> str | None:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    # Keep committed objects readable for repository tests and immediate API callers.
    with Session(engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
