import os
import sqlite3
from sqlalchemy import (create_engine, Column, Integer, String, Text,
                        DateTime, Boolean, inspect)
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.types import TypeEngine
from datetime import datetime
from typing import Optional, Dict
from eg_agent.paths import get_app_path
from eg_agent.log_config import logger as base_logger

logger = base_logger.getChild("db")

# Get database filename from environment or use default
db_filename = os.getenv("DB_FILENAME", "eg_agent.db")
db_path = get_app_path(db_filename)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{db_path}")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class TaskLog(Base):
    __tablename__ = "agent_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(36), unique=True, index=True, nullable=False)
    task_name = Column(String(255), nullable=False)
    params = Column(Text, nullable=True)
    status = Column(Text, default=TaskStatus.PENDING, nullable=False)
    result = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)
    sent_ack = Column(Boolean, default=False, nullable=False)
    sent_ack_at = Column(DateTime, nullable=True)
    received_ack = Column(Boolean, default=False, nullable=False)
    received_ack_at = Column(DateTime, nullable=True)
    for_queue = Column(Boolean, default=False, nullable=False)
    queue_name = Column(String(255), nullable=True)
    huey_task_id = Column(String(255), nullable=True)


class GlobalKeys(Base):
    __tablename__ = "global_keys"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, index=True, nullable=False)
    value = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def _get_sqlite_type(column_type: TypeEngine) -> str:
    """Convert SQLAlchemy column type to SQLite type string."""
    type_mapping = {
        Integer: "INTEGER",
        String: "VARCHAR",
        Text: "TEXT",
        DateTime: "DATETIME",
        Boolean: "BOOLEAN",
    }

    # Check for exact type match
    for sqlalchemy_type, sqlite_type in type_mapping.items():
        if isinstance(column_type, sqlalchemy_type):
            # Handle String with length
            if (isinstance(column_type, String) and
                    hasattr(column_type, 'length')):
                return f"VARCHAR({column_type.length})"
            return sqlite_type

    # Fallback: use string representation
    type_str = str(column_type)
    if "VARCHAR" in type_str or "String" in type_str:
        if "(" in type_str:
            parts = type_str.split("(")
            return parts[0].upper() + "(" + parts[1]
        return "VARCHAR(255)"
    elif "INTEGER" in type_str or "Integer" in type_str:
        return "INTEGER"
    elif "TEXT" in type_str or "Text" in type_str:
        return "TEXT"
    elif "DATETIME" in type_str or "DateTime" in type_str:
        return "DATETIME"
    elif "BOOLEAN" in type_str or "Boolean" in type_str:
        return "BOOLEAN"

    return "TEXT"  # Default fallback


def _get_existing_columns(
    table_name: str, conn: sqlite3.Connection
) -> Dict[str, Dict]:
    """Get existing columns from database table."""
    cursor = conn.cursor()
    cursor.execute(f'PRAGMA table_info({table_name})')
    columns = {}
    for row in cursor.fetchall():
        # PRAGMA returns: (cid, name, type, notnull, default_value, pk)
        col_name = row[1]
        col_type = row[2]
        not_null = row[3]
        default_val = row[4]
        is_pk = row[5]
        columns[col_name] = {
            'type': col_type,
            'notnull': not_null,
            'default': default_val,
            'pk': is_pk
        }
    return columns


def _migrate_schema():
    """Automatically migrate database schema to match SQLAlchemy models.

    This function:
    1. Creates tables if they don't exist
    2. Detects missing columns by comparing models with database
    3. Adds missing columns automatically
    4. Preserves existing data
    """
    # First, create all tables (this handles new tables)
    Base.metadata.create_all(bind=engine)

    # Get database connection for direct SQL operations
    db_path = get_app_path(db_filename)

    try:
        conn = sqlite3.connect(db_path)
        inspector = inspect(engine)

        # Check each table defined in models
        for table_name, table in Base.metadata.tables.items():
            # Check if table exists in database
            if not inspector.has_table(table_name):
                logger.debug(
                    f"Table {table_name} does not exist, skipping migration"
                )
                continue

            # Get existing columns from database
            existing_columns = _get_existing_columns(table_name, conn)

            # Get expected columns from model
            model_columns = {}
            for column in table.columns:
                model_columns[column.name] = {
                    'type': _get_sqlite_type(column.type),
                    'nullable': column.nullable,
                    'default': column.default,
                }

            # Find missing columns
            missing_columns = []
            for col_name, col_info in model_columns.items():
                if col_name not in existing_columns:
                    missing_columns.append((col_name, col_info))

            # Add missing columns
            if missing_columns:
                cursor = conn.cursor()
                for col_name, col_info in missing_columns:
                    sqlite_type = col_info['type']
                    nullable = (
                        "NULL" if col_info['nullable'] else "NOT NULL"
                    )

                    # Build ALTER TABLE statement
                    alter_sql = (
                        f'ALTER TABLE {table_name} ADD COLUMN {col_name} '
                        f'{sqlite_type} {nullable}'
                    )

                    # Add default value if specified
                    if col_info['default'] is not None:
                        default = col_info['default']
                        if hasattr(default, 'arg'):
                            # Handle SQLAlchemy default objects
                            default_val = default.arg
                            if isinstance(
                                default_val, (str, int, float, bool)
                            ):
                                if isinstance(default_val, str):
                                    alter_sql += f" DEFAULT '{default_val}'"
                                elif isinstance(default_val, bool):
                                    alter_sql += (
                                        f" DEFAULT {1 if default_val else 0}"
                                    )
                                else:
                                    alter_sql += f" DEFAULT {default_val}"

                    try:
                        cursor.execute(alter_sql)
                        logger.info(
                            f"Added column {col_name} to table {table_name}"
                        )
                    except sqlite3.OperationalError as e:
                        logger.warning(
                            f"Failed to add column {col_name} to "
                            f"{table_name}: {e}"
                        )
                        # Column might already exist (race condition)

                conn.commit()
                cursor.close()

        conn.close()
        logger.info("Schema migration completed")

    except Exception as e:
        logger.error(f"Error during schema migration: {e}", exc_info=True)
        # Don't raise - allow app to continue even if migration fails
        # The table creation above should still work


def init_db():
    """Create all tables and migrate schema if needed.

    This function:
    1. Creates tables if they don't exist
    2. Automatically detects and adds missing columns
    3. Preserves existing data
    """
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized and tables created (if not exist)")

    # Run automatic migrations to add any missing columns
    _migrate_schema()


def get_db():
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        logger.debug("DB session closed")


def log_task(db, task_id, task_name, params, for_queue=False, queue_name=None):
    """Insert new task as PENDING."""
    new_task = TaskLog(
        task_id=task_id,
        task_name=task_name,
        params=str(params),
        for_queue=for_queue,
        queue_name=queue_name
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    logger.info("Logged task %s (%s)", task_id, task_name)
    return new_task


def update_task_status(db, task_id, status, result=None):
    """Update task status."""
    task = db.query(TaskLog).filter(TaskLog.task_id == task_id).first()
    if task:
        task.status = status
        if result:
            task.result = str(result)
        db.commit()
        db.refresh(task)
        logger.info("Updated task %s status to %s", task_id, status)
    return task


def mark_sent_ack(db: Session, task_id: str) -> Optional[TaskLog]:
    """Mark that the task ACK was sent."""
    if not isinstance(db, Session):
        db = next(db)

    task = db.query(TaskLog).filter(TaskLog.task_id == task_id).first()
    if task:
        task.sent_ack = True
        task.sent_ack_at = datetime.utcnow()
        db.commit()
        db.refresh(task)
        logger.debug("Marked sent ACK for task %s", task_id)
        return task
    return None


def mark_received_ack(db: Session, task_id: str) -> Optional[TaskLog]:
    """Mark that the task ACK was received."""
    if not isinstance(db, Session):
        db = next(db)

    task = db.query(TaskLog).filter(TaskLog.task_id == task_id).first()
    if task:
        task.received_ack = True
        task.received_ack_at = datetime.utcnow()
        db.commit()
        db.refresh(task)
        logger.debug("Marked received ACK for task %s", task_id)
        return task
    return None


def update_huey_task_id(db: Session, task_id: str,
                        huey_task_id: str) -> Optional[TaskLog]:
    """Update the Huey task ID for a queued task."""
    if not isinstance(db, Session):
        db = next(db)

    task = db.query(TaskLog).filter(TaskLog.task_id == task_id).first()
    if task:
        task.huey_task_id = huey_task_id
        db.commit()
        db.refresh(task)
        logger.debug("Updated Huey task ID for task %s: %s",
                     task_id, huey_task_id)
        return task
    return None


def get_task_by_id(db: Session, task_id: str) -> Optional[TaskLog]:
    """Get a task by its task_id."""
    if not isinstance(db, Session):
        db = next(db)

    return db.query(TaskLog).filter(TaskLog.task_id == task_id).first()


def get_global_key(db: Session, key: str) -> Optional[str]:
    """Get a value from global_keys table by key name."""
    if not isinstance(db, Session):
        db = next(db)
    row = db.query(GlobalKeys).filter(GlobalKeys.key == key).first()
    return row.value if row and row.value else None


def set_global_key(db: Session, key: str, value: str) -> Optional[GlobalKeys]:
    """Set or update a value in global_keys table."""
    if not isinstance(db, Session):
        db = next(db)
    row = db.query(GlobalKeys).filter(GlobalKeys.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        row = GlobalKeys(key=key, value=value)
        db.add(row)
    db.commit()
    db.refresh(row)
    logger.debug("Set global key %s", key)
    return row
