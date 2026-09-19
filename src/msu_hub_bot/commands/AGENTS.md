# Command behavior

- Registrations and access filters live in [routing.py](../routing.py); speech and Wolfram handlers also live in [providers/wit.py](../providers/wit.py) and [providers/wolfram.py](../providers/wolfram.py).
- Follow each feature through aliases, callbacks, FSM steps, edited messages and automatic triggers before changing or retiring it.
- Preserve selected inside jokes, personal sticker replies and small novelty commands; polish reliability without sanding away their character.
- Keep Russian and English copy natural, warm and specific; short acknowledgments suit small actions, while useful results may need explanation or links.
- Preserve deliberate playful behavior: `/tenet` quiz copies intentionally use answer index 0; do not turn the joke into strict quiz validation.
- Live-location weather uses an in-memory 15-minute throttle per chat/message, without persistent timers or database state.
- Preserve aliases unless the user explicitly chooses to change them; audit recommendations alone do not authorize removal.
- Keep error replies deliberate and outputs bounded, and avoid embedding provider or storage details in command presentation.
- Chess, geoguess, raffle, reminder, reaction and Mini App flows preserve the author’s invoking message, including aliases and leaderboards; cleanup targets temporary bot UI.
- Persist JSON-compatible Pydantic drafts in FSM; scope conversations per user/chat/topic. `/cancel` clears drafts without cancelling already-started work.
