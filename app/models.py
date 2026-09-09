import uuid
from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base

# Definimos _uuid para satisfacer la importación que pide models_ops
_uuid = uuid.uuid4

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)

class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)

class Plan(Base):
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)

class Suscripcion(Base):
    __tablename__ = "suscripciones"
    id = Column(Integer, primary_key=True, index=True)
