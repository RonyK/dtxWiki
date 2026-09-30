from sqlalchemy import MetaData
from sqlalchemy.engine import Engine


def drop_reflected_postgres_metadata(engine: Engine) -> None:
    """Drop a dedicated test database's tables using real FK names.

    The ORM contains intentional bidirectional foreign-key cycles. PostgreSQL
    assigns names to constraints declared without one, but those generated
    names are absent from ``Base.metadata`` and make a direct ``drop_all``
    unsortable. Reflection recovers the database names so SQLAlchemy can drop
    the cyclic constraints before dropping their tables.
    """

    metadata = MetaData()
    metadata.reflect(bind=engine)
    metadata.drop_all(bind=engine)
