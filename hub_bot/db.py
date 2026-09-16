from abc import ABC
from typing import Optional

from common.db.edb import EDBModelBase


class MSUHubModule(EDBModelBase, ABC):
    @classmethod
    def module(cls) -> str:
        return 'msu_hub'


class EcosystemChat(MSUHubModule):
    @classmethod
    def pk_field(cls) -> str:
        return 'chat_id'

    chat_id: int
    name: str
    section: str
    is_hidden: bool
    username_alias: Optional[str] = None
    members: Optional[int] = None
    pinned_message_id: Optional[int] = None


class VkTgModule(EDBModelBase, ABC):
    @classmethod
    def module(cls) -> str:
        return 'vk_tg'


class VkWallPosting(VkTgModule):
    owner_id: int
    chat_id: int
    last_post_id: int
    with_reposts: bool
    with_header: bool
    is_suspended: bool
    description: Optional[str] = None
