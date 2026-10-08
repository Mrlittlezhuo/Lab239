import json

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeMeta, declarative_base, sessionmaker

from chatchat.settings import Settings


engine = create_engine(
    Settings.basic_settings.SQLALCHEMY_DATABASE_URI,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base: DeclarativeMeta = declarative_base()
