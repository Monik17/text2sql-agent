"""
db.py — MySQL helpers for schema extraction and connection testing.
Uses LangChain's SQLDatabase wrapper for schema introspection.
"""

from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine, text
from typing import Tuple, Union


def get_db(db_uri: str) -> SQLDatabase:
    """Return a LangChain SQLDatabase object."""
    return SQLDatabase.from_uri(db_uri)


def get_schema(db_uri: str) -> str:
    """Return the CREATE TABLE schema string for all tables."""
    db = get_db(db_uri)
    return db.get_table_info()


def test_connection(db_uri: str) -> Tuple[bool, Union[list, str]]:
    """
    Test the MySQL connection.
    Returns (True, [table names]) on success, (False, error_message) on failure.
    """
    try:
        engine = create_engine(db_uri, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            rows = conn.execute(text("SHOW TABLES")).fetchall()
            tables = [r[0] for r in rows]
        return True, tables
    except Exception as e:
        return False, str(e)
