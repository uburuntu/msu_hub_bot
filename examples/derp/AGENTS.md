# Derp consumer example

- `inline.py` replaces the complete PR29 inline router using its concrete feature service. Preserve native UUID identity, privacy projection, translated controls and sender formatting; keep its router key `inline` for native model loading.
- Keep other features in the native host router hierarchy. Do not add forwarding classes or replacement domain protocols to make native handlers appear smaller.
- Run `tests/` here only in an isolated Derp environment; its conftest installs synthetic settings and blocks network before test imports. The ordinary Hub test/import graph must not require Derp. Strict-type the optional module against that environment.
