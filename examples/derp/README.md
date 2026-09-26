# Derp inline feature

`inline.py` replaces the complete inline router from Derp PR29: empty-query help,
question previews and chosen-result answers. It uses the existing concrete
`InlineChatFeatureService`, native account models, privacy projection and UUID
request identity. The service continues to own consent, execution and accounting.
Derp's legacy main branch has a different service interface and is not the target.

The feature shares placeholder construction and translates outcomes through one
edit path. It retains the native `MessageSender` for Markdown/HTML conversion,
inline truncation and the host's HTML-error fallback. TeleForge contributes typed,
inspectable declarations and the shared test entrypoint; it does not supply new
accounting guarantees or apply its managed output policy to this native sender.

Import this optional module only in a compatible Derp environment. The validated
environment uses Python 3.14 and aiogram 3.31; the authoritative dependency range
is in `packages/teleforge/pyproject.toml`. PR29's original aiogram 3.30 lock needs a
separate dependency upgrade before using TeleForge. The Hub import/test graph
does not need Derp.

During native host construction, keep its middleware and replace the final
`dispatcher.include_routers(*APPLICATION_ROUTERS)` call with:

```python
from derp.application import APPLICATION_ROUTERS
from derp.handlers.inline import router as native_inline_router
from teleforge import App

from examples.derp.inline import InlineAnswers

app = App(InlineAnswers(runtime.inline_chat_service))
inline_router = app.build_router()
dispatcher.include_routers(*(inline_router if router is native_inline_router else router for router in APPLICATION_ROUTERS))
```

Run the existing host loop within `app.lifespan()`. This replacement belongs
before the routers are attached; do not append it after the normal dispatcher
constructor has already registered the native inline router. The feature key
`inline` preserves native `RouteDependencyMiddleware` model loading without extra
plans. Keep the native i18n and session middleware installed.

All other routes remain in `APPLICATION_ROUTERS`. In particular, paid images,
saved-result resend and the complete image approval family retain their native
handlers and services. The approval router is already nested under the native
chat router; do not attach it separately. This avoids duplicating aliases,
upload flags, consent controls and commerce-policy forwarding in another class.
Full contextual chat, payments and their workers also remain native.

The optional tests use the real inline service and native model-loading/session
middleware with synthetic SQL, accounting, provider and Telegram edges. They
cover both placeholder variants, selected answers, privacy changes, all native
failure outcomes, request identity and formatted output. Run them in a separate
process from an isolated PR29 checkout with its dependencies and TeleForge
installed, passing its pytest configuration explicitly:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /path/to/derp/.venv-teleforge/bin/python -m pytest -p pytest_asyncio.plugin -c /path/to/derp/pyproject.toml /path/to/hub/examples/derp/tests
```

The test conftest refuses environment files, sets synthetic configuration and
blocks network before test imports. These tests establish native integration
across synthetic edges; they do not establish PostgreSQL settlement, process
restart behavior, a production dependency upgrade or deployment readiness.
