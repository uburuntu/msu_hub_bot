# Bot application

- [routing.py](routing.py) registers ordered feature-router fragments; [app.py](app.py) owns clients, polling and shutdown. Preserve global first-match order when regrouping routes.
- Use canonical package imports and explicit handler dependencies. Imports must not open clients or start work; the installed CLI needs no path injection.
- Middleware owns common preferences, update history, automatic previews, topic isolation and telemetry. Keep handler keys stable and release state isolation only at audited terminal selections or consumed drafts.
- [events.py](events.py) manages chat membership/directory events; [db.py](db.py) contains bot-specific models; text lives in [texts.py](texts.py) and command modules.
- Preserve the friends' Swiss-army bot born in MSU chats: broad utility, playful surprises and the inside jokes chosen for retention all belong.
- Write natural Russian and English; keep quick replies concise and include useful detail or links when the task warrants them.
- Keep startup, shutdown, route registration and user-facing behavior consistent when changing a service boundary.
