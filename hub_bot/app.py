from dataclasses import dataclass
from multiprocessing import current_process

from applets import AppDvach, AppWit, AppWolfram, AppJDoodle, AppEdgeDB
from common.applets import AppBot, AppCPUExecutor, AppLogger, AppScheduler, AppVK, AppHealthCheck
from common.tg.middlewares.check_gets import CheckGets
from common.tg.middlewares.settings import SettingsMiddleware
from common.tg.middlewares.skip777000 import Skip777000
from common.tg.middlewares.updates import UpdatesMiddleware
from common.tg.middlewares.viewer import ViewerMiddleware
from config import config
from events import EventsMiddleware, EcosystemManager


@dataclass
class AppHubBot(AppBot, AppVK, AppWolfram, AppDvach, AppWit, AppJDoodle,
                AppLogger, AppCPUExecutor, AppScheduler, AppHealthCheck, AppEdgeDB):
    em: EcosystemManager = None

    def post_init(self):
        self.dp.middleware.setup(SettingsMiddleware(self.edgedb))

        self.dp.middleware.setup(Skip777000())
        self.dp.middleware.setup(CheckGets())

        events_tracking_middleware = EventsMiddleware(self.bot, self.edgedb, self.config.events_chat_id)
        self.em = events_tracking_middleware.em
        self.dp.middleware.setup(events_tracking_middleware)

        updates_middleware = UpdatesMiddleware(self.edgedb)
        self.dp.middleware.setup(updates_middleware)

        viewer_middleware = ViewerMiddleware(self.bot, self.vk_api, executor=self.cpu_executor)
        self.dp.middleware.setup(viewer_middleware)

        self.wit.executor = self.cpu_executor


app = AppHubBot()
if current_process().name == 'MainProcess':
    app.init_all(config)

bot = app.bot
dp = app.dp
logger = app.logger
redis = app.redis
db = app.edgedb

em = app.em
dvach = app.dvach
vk_api = app.vk_api
wit = app.wit
wolfram = app.wolfram
jdoodle = app.jdoodle

cpu_executor = app.cpu_executor
events_chat_id = config.events_chat_id
