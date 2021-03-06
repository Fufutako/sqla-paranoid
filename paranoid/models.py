import itertools
import logging
import operator
import weakref

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    event,
)
from sqlalchemy.ext.declarative import (
    declared_attr,
    declarative_base,
)
from sqlalchemy.orm import (
    Mapper as BaseMapper,
    Query as SqlaQuery,
    Session as SqlaSession,
)
from sqlalchemy.orm.relationships import RelationshipProperty
from sqlalchemy.sql.expression import BinaryExpression


logger = logging.getLogger('paranoid')


class Mapper(BaseMapper):
    def _configure_property(self, key, prop, init=True, setparent=True):
        if isinstance(prop, RelationshipProperty):
            prop.query_class = Query

        super()._configure_property(key, prop, init, setparent)


def query_factory(BaseQuery):
    class Query(BaseQuery):
        # This allows softdelete passive criterion for few
        # methods such as `get` or `select_from`.
        _enable_assertions = False

        def __new__(cls, *entities, **kw):
            if entities:
                query = parent = BaseQuery(*entities, **kw)
            else:
                query = parent = BaseQuery.__new__(BaseQuery, **kw)

            query.__entities = entities
            query.__parent__ = parent
            query.__class__ = cls
            query = query.restrict()
            return query

        def restrict(self):
            query = self
            for entity in self.__entities:
                deleted_at = None

                if isinstance(entity, tuple):
                    entity = entity[0]

                if hasattr(entity, 'deleted_at'):
                    deleted_at = entity.deleted_at

                if hasattr(entity, 'c'):
                    deleted_at = entity.c.get('deleted_at')

                if deleted_at is not None:
                    query = query.filter(deleted_at.__eq__(None))
                    query._soft_delete = deleted_at

            return query

    return Query


Query = query_factory(SqlaQuery)


def session_factory(BaseSesion):
    class Session(BaseSesion):
        def delete(self, obj):
            if hasattr(obj, '__softdelete__') and obj.__softdelete__:
                obj.deleted_at = datetime.utcnow()
                return True

            return super().delete(obj)

    return Session


Session = session_factory(SqlaSession)


def apply_soft_delete(expression, deleted_at):
    if deleted_at is not None:
        expression = BinaryExpression(
            expression,
            deleted_at.is_(None),
            operator.and_,
        )

    return expression


BaseModel = declarative_base()


def model_factory(BaseModel=BaseModel):
    class Model(BaseModel):
        __abstract__ = True
        __softdelete__ = False

        @declared_attr
        def deleted_at(cls):
            if cls.__softdelete__:
                return Column(DateTime(timezone=True), nullable=True)

        def delete(self):
            logger.info("deleting %r" % self)

        @declared_attr
        def __mapper_cls__(cls):
            return Mapper

    @event.listens_for(Model, "mapper_configured", propagate=True)
    def configure_soft_delete(mapper, cls_, mappers=weakref.WeakSet()):
        mappers.add(mapper)

        @event.listens_for(BaseMapper, 'after_configured')
        def receive_after_configured():
            relationships = itertools.chain(
                *(mapper.relationships.values() for mapper in mappers)
            )

            mappers.clear()

            for relationship in relationships:
                if relationship.primaryjoin is None:
                    continue

                join_condition = relationship._join_condition
                local_remote_pairs = list(join_condition.local_remote_pairs)

                if relationship.secondaryjoin is not None:
                    for column in local_remote_pairs.pop():
                        join_condition.secondaryjoin = apply_soft_delete(
                            join_condition.secondaryjoin,
                            column.table.c.get('deleted_at'),
                        )

                rc = local_remote_pairs.pop()[1]
                join_condition.primaryjoin = apply_soft_delete(
                    join_condition.primaryjoin,
                    rc.table.c.get('deleted_at'),
                )

    return Model


Model = model_factory()
