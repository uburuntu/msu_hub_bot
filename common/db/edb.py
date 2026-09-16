from msu_hub_bot.settings import Settings, settings

import datetime
from abc import ABC
from functools import lru_cache
from typing import Any, List, Optional, TypeVar, Generic, Dict, Type
from uuid import UUID

import edgedb
import pytz
from aiocache import cached
from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler
from pydantic_core import core_schema

from common import json




class EdgeDB:
    def __init__(self, database: str = 'msu_hub', *, config: Settings = settings) -> None:
        self.client = edgedb.create_async_client(
            dsn=config.edgedb_dsn,
            tls_ca=config.edgedb_tls_ca or None,
            tls_security=config.edgedb_tls_security,
        )

    @classmethod
    def cast_type(cls, v) -> str:
        convert = {
            dict: 'json',
            int: 'int64',
            UUID: 'uuid',
            datetime.datetime: 'std::datetime',
        }.get(type(v), type(v).__name__)
        return f'<{convert}>'

    @classmethod
    def cast_value(cls, v):
        if isinstance(v, dict):
            return json.dumps(v)
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, datetime.datetime):
            return v.astimezone(pytz.UTC)
        return v

    @classmethod
    def cast_value_bulk(cls, v):
        # Skip Json and datetime as iso
        if isinstance(v, dict):
            return json.dumps(v)
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, datetime.datetime):
            return v.isoformat() + '+00'
        return v

    async def client_query(self, *args, **kwargs):
        return await self.client.query(*args, **kwargs)

    def handle_kwargs(self, kwargs):
        args = ','.join(f'{k}:={self.cast_type(v)}${k}' for k, v in kwargs.items() if v is not None)
        kwargs = {k: self.cast_value(v) for k, v in kwargs.items() if v is not None}
        return args, kwargs

    async def get(self, type_name: str, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        filter_part = f'filter {{{args}}}' if args else ''
        return await self.client.query(f'select {type_name} {filter_part};', **kwargs)

    async def get_single(self, type_name: str, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        return await self.client.query_single(f'select {type_name} filter {{{args}}} limit 1;', **kwargs)

    async def insert(self, type_name: str, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        return await self.client_query(f'insert {type_name} {{{args}}};', **kwargs)

    async def insert_skip_conflict(self, type_name: str, unique_field: str, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        return await self.client_query(f'insert {type_name} {{{args}}} '
                                       f'unless conflict on .{unique_field} '
                                       f'else (select {type_name});', **kwargs)

    async def upsert(self, type_name: str, unique_field: str, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        return await self.client_query(f'insert {type_name} {{{args}}} '
                                       f'unless conflict on .{unique_field} '
                                       f'else (update {type_name} set {{{args}}});', **kwargs)

    async def update(self, type_name: str, filter_field: str, filter_value, **kwargs):
        args, kwargs = self.handle_kwargs(kwargs)
        return await self.client_query(f'update {type_name} '
                                       f'filter .{filter_field} = {filter_value} '
                                       f'set {{{args}}};', **kwargs)

    def handle_bulk_data(self, data: List[dict]):
        args = ','.join(f'{k}:={self.cast_type(v)}item[\'{k}\']' for k, v in data[0].items() if v is not None)
        data = [{k: self.cast_value_bulk(v) for k, v in d.items() if v is not None} for d in data]
        return args, json.dumps(data)

    async def bulk_insert(self, type_name: str, data: List[dict]):
        # Huevit s sparse dannimi s nullami
        args, data = self.handle_bulk_data(data)
        return await self.client.query(f'with raw_data := <json>$data '
                                       f'for item in json_array_unpack(raw_data) union ('
                                       f'insert {type_name} {{{args}}});', data=data)

    async def bulk_insert_skip_conflict(self, type_name: str, unique_field: str, data: List[dict]):
        # Huevit s sparse dannimi s nullami
        args, data = self.handle_bulk_data(data)
        return await self.client.query(f'with raw_data := <json>$data '
                                       f'for item in json_array_unpack(raw_data) union ('
                                       f'insert {type_name} {{{args}}} '
                                       f'unless conflict on .{unique_field} '
                                       f'else (select {type_name})'
                                       f');', data=data)

    async def close(self) -> None:
        await self.client.aclose()


def obj_to_dict(o: edgedb.Object) -> dict:
    return {field: getattr(o, field) for field in dir(o)}


class Args:
    def __init__(self, kwargs, for_filter: bool = False):
        args, kwargs = self.handle_kwargs(kwargs, for_filter)
        self.args = args
        self.kwargs = kwargs

    @classmethod
    def cast_type(cls, v) -> str:
        convert = {
            dict: 'json',
            int: 'int64',
            UUID: 'uuid',
            datetime.datetime: 'std::datetime',
        }.get(type(v), type(v).__name__)
        return f'<{convert}>'

    @classmethod
    def cast_value(cls, v):
        if isinstance(v, dict):
            return json.dumps(v)
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, datetime.datetime):
            return v.astimezone(pytz.UTC)
        return v

    @classmethod
    def format(cls, k, v, for_filter: bool):
        if for_filter:
            return f'.{k} = {cls.cast_type(v)}${k}'
        return f'{k} := {cls.cast_type(v)}${k}'

    def handle_kwargs(self, kwargs, for_filter: bool):
        sep = ' and ' if for_filter else ', '
        args = sep.join(self.format(k, v, for_filter)
                        for k, v in kwargs.items()
                        if v is not None)
        kwargs = {k: self.cast_value(v)
                  for k, v in kwargs.items()
                  if v is not None}
        return args, kwargs

    def __add__(self, other):
        if not isinstance(other, type(self)):
            raise ValueError()
        return self.args, other.args, {**self.kwargs, **other.kwargs}


T = TypeVar('T')
V = TypeVar('V')


class QueryBuilder(Generic[T]):
    def __init__(self, db: EdgeDB, model_class: T, type_name: str, fields: str, pk_field: str, pk_value: V = None):
        self.db = db
        self.model_class = model_class
        self.type_name = type_name
        self.fields = fields
        self.pk_field = pk_field  # todo: try tuple for multi pks
        self.pk_value = pk_value

    async def query(self, query: str, *args, **kwargs):
        return await self.db.client.query(query, *args, **kwargs)

    async def query_single(self, query: str, *args, **kwargs):
        return await self.db.client.query_single(query, *args, **kwargs)

    async def _get_all(self):
        query = f'select {self.type_name} {{{self.fields}}};'
        return await self.query(query)

    async def _get_by_pk(self, pk):
        a = Args({self.pk_field: pk}, for_filter=True)
        query = f'select {self.type_name} {{{self.fields}}} filter {{{a.args}}} limit 1;'
        return await self.query_single(query, **a.kwargs)

    async def count(self, filter_part: str = None) -> int:
        filter_part = f' filter {filter_part}' if filter_part else ''
        query = f'select count({self.type_name}{filter_part});'
        return await self.query_single(query)

    async def get_all(self) -> List[T]:
        raws = await self._get_all()
        return [self.model_class.model_validate(obj_to_dict(raw)) for raw in raws]

    async def get(self, pk: V) -> T:
        raw = await self._get_by_pk(pk)
        return self.model_class.model_validate(obj_to_dict(raw))

    async def insert(self, **kwargs):
        return await self.db.insert(self.type_name, **kwargs)

    async def update(self, pk: V, **kwargs) -> T:
        a1, a2, kwargs = Args({self.pk_field: pk}, for_filter=True) + Args(kwargs)
        return await self.query(f'update {self.type_name} '
                                f'filter {{{a1}}} '
                                f'set {{{a2}}};', **kwargs)

    async def update2(self, pk1_name: str, pk1: V, pk2_name: str, pk2: V, **kwargs) -> T:
        a1, a2, kwargs = Args({pk1_name: pk1, pk2_name: pk2}, for_filter=True) + Args(kwargs)
        return await self.query(f'update {self.type_name} '
                                f'filter {{{a1}}} '
                                f'set {{{a2}}};', **kwargs)

    async def upsert2(self, pk1_name: str, pk2_name: str, **kwargs) -> T:
        a = Args(kwargs)
        return await self.query(f'insert {self.type_name} {{{a.args}}} '
                                f'unless conflict on ( (.{pk1_name}, .{pk2_name}) ) '
                                f'else (update {self.type_name} set {{{a.args}}});', **a.kwargs)

    async def delete(self, pk: V):
        a = Args({self.pk_field: pk}, for_filter=True)
        return await self.query(f'delete {self.type_name} '
                                       f'filter {{{a.args}}}', **a.kwargs)

    # Not general

    @cached(ttl=300)
    async def get_all_cached(self) -> Dict[int, T]:
        objects = await self.get_all()
        return {getattr(obj, self.pk_field): obj for obj in objects}

    async def get_cached(self, pk: V) -> Optional[T]:
        objects = await self.get_all_cached()
        return objects.get(pk)

    async def exist_cached(self, pk: V) -> bool:
        return bool(await self.get_cached(pk))

    async def exist(self, pk: V) -> bool:
        return bool(await self._get_by_pk(pk))


class EDBDict(dict[str, Any]):
    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        # Stored JSON historically accepts objects, arrays, scalars and null.
        return core_schema.no_info_plain_validator_function(cls.validate)

    @classmethod
    def validate(cls, v):
        if isinstance(v, str):
            return json.loads(v)
        return v


class EDBModelBase(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, hide_input_in_errors=True)

    id: UUID

    @classmethod
    def fields(cls):
        return ', '.join(cls.model_fields)

    @classmethod
    def type_name(cls) -> str:
        return f'{cls.module()}::{cls.class_name()}'

    @classmethod
    @lru_cache()
    def query(cls: Type[T], db: EdgeDB) -> QueryBuilder[T]:
        return QueryBuilder(db, cls, cls.type_name(), cls.fields(), cls.pk_field())

    # def query(self: T, db: EdgeDB) -> QueryBuilder[T, V]:
    #     return QueryBuilder(db, type(self), self.type_name(), self.fields(), self.pk_field(),
    #                         getattr(self, self.pk_field()))

    @classmethod
    def module(cls) -> str:
        return 'default'

    @classmethod
    def class_name(cls) -> str:
        return cls.__name__

    @classmethod
    def pk_field(cls) -> str:
        return 'id'


class TelegramModule(EDBModelBase, ABC):
    @classmethod
    def module(cls) -> str:
        return 'telegram'


class UpdateDB(TelegramModule):
    @classmethod
    def class_name(cls) -> str:
        return 'BotUpdate'

    data: EDBDict
    handled: bool


class UserDB(TelegramModule):
    @classmethod
    def class_name(cls) -> str:
        return 'User'

    @classmethod
    def pk_field(cls) -> str:
        return 'user_id'

    user_id: int
    is_bot: bool
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    language_code: Optional[str] = None


class ChatDB(TelegramModule):
    @classmethod
    def class_name(cls) -> str:
        return 'Chat'

    @classmethod
    def pk_field(cls) -> str:
        return 'chat_id'

    chat_id: int
    type: str
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    metadata: EDBDict
