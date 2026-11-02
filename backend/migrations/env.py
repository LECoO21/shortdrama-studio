from alembic import context
from sqlalchemy import create_engine

config = context.config
if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
