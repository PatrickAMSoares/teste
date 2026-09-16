"""Camada de persistência local (SQLite). O aplicativo não usa servidor algum."""

from .database import Database, SCHEMA_VERSION
from .repository import Repository

__all__ = ["Database", "Repository", "SCHEMA_VERSION"]
