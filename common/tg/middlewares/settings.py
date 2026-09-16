import cachetools
from aiogram.dispatcher.middlewares import LifetimeControllerMiddleware
from aiogram.types import Chat
from pydantic import BaseSettings, root_validator, Extra
from throttler import ThrottlerSimultaneous

from common import json
from common.db.edb import EdgeDB, ChatDB


class Settings(BaseSettings):
    auto_speech_recognition: bool = True
    auto_video_links: bool = True
    with_nsfw: bool = False

    _is_dirty: bool = False
    _chat_id: int = None

    class Config:
        validate_all = True
        extra = Extra.allow
        validate_assignment = True
        json_loads = json.loads
        json_dumps = json.dumps

    @classmethod
    async def create(cls, db: EdgeDB, chat_id: int) -> 'Settings':
        chat_db = await ChatDB.query(db).get(chat_id)
        metadata = chat_db.metadata if isinstance(chat_db.metadata, dict) else {}
        settings = dict(metadata.get('settings') or {})
        settings['_chat_id'] = chat_id
        obj = cls.parse_obj(settings)
        obj.__dict__['_is_dirty'] = False  # to skip validator
        return obj

    @root_validator
    def set_as_dirty(cls, values):
        values['_is_dirty'] = True
        return values

    async def save(self, db: EdgeDB, force=False) -> 'Settings':
        if self._is_dirty or force:
            snapshot = self.dict(exclude={'_is_dirty', '_chat_id'})
            chat_db = await ChatDB.query(db).get(self._chat_id)
            if not isinstance(chat_db.metadata, dict):
                chat_db.metadata = {}
            chat_db.metadata['settings'] = snapshot
            await ChatDB.query(db).update(self._chat_id, metadata=chat_db.metadata)
            # A handler may change preferences while the database write is pending.
            if self.dict(exclude={'_is_dirty', '_chat_id'}) == snapshot:
                self.__dict__['_is_dirty'] = False  # to skip validator
        return self


class SettingsMiddleware(LifetimeControllerMiddleware):
    skip_patterns = ['error', 'update']

    def __init__(self, db: EdgeDB):
        super().__init__()
        self.db = db
        self.proxies = cachetools.LRUCache(maxsize=128)
        self.throttler = ThrottlerSimultaneous(count=1)

    async def proxy(self, chat: Chat) -> Settings:
        async with self.throttler:
            if chat.id not in self.proxies:
                # Settings are needed before the asynchronous update archive runs.
                # Insert-on-conflict preserves preferences already stored by another update.
                await self.db.insert_skip_conflict(
                    'telegram::Chat', 'chat_id', chat_id=chat.id, type=chat.type,
                    title=chat.title, username=chat.username,
                    first_name=chat.first_name, last_name=chat.last_name,
                )
                self.proxies[chat.id] = await Settings.create(self.db, chat.id)
        return self.proxies[chat.id]

    async def pre_process(self, obj, data, *args):
        chat = getattr(obj, 'chat', None) or getattr(getattr(obj, 'message', None), 'chat', None)
        if not isinstance(chat, Chat):
            return
        data['settings'] = await self.proxy(chat)

    async def post_process(self, obj, data, *args):
        proxy = data.get('settings', None)
        if isinstance(proxy, Settings):
            async with self.throttler:
                await proxy.save(self.db)
