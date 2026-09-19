# Third-party materials

Application code is distributed under GPL-3.0-only. These font files retain
their own licenses; the application license does not replace them.

| Asset | Origin and terms |
| --- | --- |
| Liberation Serif Regular and Sans Regular 2.1.5 | Liberation Fonts; SIL Open Font License 1.1. Retrieved from Debian's `fonts-liberation2` 2.1.5-1 package and checked against Debian's SHA256 metadata. See `licenses/Liberation.txt`. |
| Lobster Regular | Pablo Impallari; SIL Open Font License 1.1. See `licenses/Lobster.txt`. |
| Ubuntu Mono Regular | Canonical; Ubuntu Font Licence 1.0. See `licenses/UbuntuMono.txt`. |
| Debate dataset | Bundled with the maintainer's confirmation that it may be redistributed. |

Liberation Serif font SHA256:
`29d12439831b7f59194efec85872f24f54eff05738933f9a860220d2abff88ba`.

Liberation Sans font SHA256:
`8d91388f1d3604b3b8ae0e3ee2d140e50cd6122f9214514f4aca772540a4076d`.

The ACRCloud Python SDK remains an external, pinned Git dependency. Its native
binaries are not vendored in this repository. Deployment images are transferred
privately and are not published for redistribution. All other dependencies
retain their respective upstream licenses.

## Imageboard SDK

The imageboard integration includes [api2ch 1.2.1](https://github.com/uburuntu/api2ch), copyright 2020 Ramzan Bekbulatov, under the [MIT license](src/msu_hub_bot/providers/_api2ch/LICENSE).

Its source is isolated in `src/msu_hub_bot/providers/_api2ch`. Imports use the local package namespace and `pydantic.v1`; the SDK implementation is otherwise unchanged. The adjacent partial type stub describes the operations used by this bot. Application code imports the boundary in `src/msu_hub_bot/providers/dvach.py` and uses Pydantic 2 for its own models.

## U²-NetP foreground model

The image build includes U²-NetP weights from [U²-Net](https://github.com/xuebinqin/U-2-Net), by Xuebin Qin and contributors, under the [Apache License 2.0](licenses/U-2-Net.txt). The fixed ONNX conversion is distributed by [rembg](https://github.com/danielgatis/rembg). Its source and checksum are maintained in `media/background_model.py`; model files are fetched explicitly during the build, never during image processing.
