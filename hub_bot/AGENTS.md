# Bot application

- [main.py](main.py) registers routes, [app.py](app.py) creates global services, and [applets.py](applets.py) wires bot-specific clients.
- [msu_hub_bot/cli.py](../msu_hub_bot/cli.py) adds this directory to the import path; short imports such as `app`, `commands` and `utils` depend on that bootstrap.
- Importing `app.py` initializes services, so inspect or stub those dependencies before using application imports in isolated checks.
- [events.py](events.py) manages chat membership/directory events; [db.py](db.py) contains bot-specific models; text lives in [texts.py](texts.py) and command modules.
- Preserve the friends' Swiss-army bot born in MSU chats: broad utility, playful surprises and the inside jokes chosen for retention all belong.
- Write natural Russian and English; keep quick replies concise and include useful detail or links when the task warrants them.
- Keep startup, shutdown, route registration and user-facing behavior consistent when changing a service boundary.
