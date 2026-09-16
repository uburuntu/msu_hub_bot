# Command behavior

- Handlers live here, but registrations and access filters live in [main.py](../main.py); speech and Wolfram handlers also live in [utils/](../utils/).
- Follow each feature through aliases, callbacks, FSM steps, edited messages and automatic triggers before changing or retiring it.
- Preserve selected inside jokes, personal sticker replies and small novelty commands; polish reliability without sanding away their character.
- Keep Russian and English copy natural, warm and specific; short acknowledgments suit small actions, while useful results may need explanation or links.
- Retaining a feature does not waive correctness; distinguish its intended behavior from defects in its implementation.
- Preserve aliases unless the user explicitly chooses to change them; audit recommendations alone do not authorize removal.
- Keep error replies deliberate and outputs bounded, and avoid embedding provider or storage details in command presentation.
