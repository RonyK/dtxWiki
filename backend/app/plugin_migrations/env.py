"""Core-owned Alembic environment for isolated plugin migration chains."""

from alembic import context

config = context.config
context.configure(
    connection=config.attributes["connection"],
    target_metadata=config.attributes["target_metadata"],
    version_table=config.attributes["version_table"],
    transactional_ddl=True,
)
with context.begin_transaction():
    context.run_migrations()
