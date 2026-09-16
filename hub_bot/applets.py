from dataclasses import dataclass

from api2ch import Api2chAsync

from common.applets import AppBase
from common.db.edb import EdgeDB
from utils.jdoodle import ManyJDoodle
from utils.wit import Wit
from utils.wolfram import WolframAPI


@dataclass
class AppEdgeDB(AppBase):
    edgedb: EdgeDB = None

    def init(self):
        self.edgedb = EdgeDB()
        return self

    async def on_shutdown(self):
        await self.edgedb.close()

    async def on_startup(self):
        await self.edgedb.client.query_single('SELECT 1')


@dataclass
class AppWolfram(AppBase):
    wolfram: WolframAPI = None

    def init(self):
        self.wolfram = WolframAPI(self.config.wolfram_token)
        return self

    async def on_shutdown(self):
        await self.wolfram.close()


@dataclass
class AppWit(AppBase):
    wit: Wit = None

    def init(self):
        self.wit = Wit(self.config.wit_tokens)
        return self

    async def on_shutdown(self):
        await self.wit.close()


@dataclass
class AppJDoodle(AppBase):
    jdoodle: ManyJDoodle = None

    def init(self):
        self.jdoodle = ManyJDoodle(self.config.jdoodle_tokens)
        return self

    async def on_shutdown(self):
        await self.jdoodle.close()


@dataclass
class AppDvach(AppBase):
    dvach: Api2chAsync = None

    def init(self):
        self.dvach = Api2chAsync()
        return self

    async def on_shutdown(self):
        await self.dvach.close()
