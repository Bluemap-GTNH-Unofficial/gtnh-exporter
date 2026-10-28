# Third-party notices

This mod is MIT ([LICENSE](LICENSE)). It is unofficial and not affiliated with BlueMap, GT New Horizons, Mojang or
Microsoft, or the authors of any mod named here. The complete list for the whole project is the umbrella repository's
[NOTICE.md](https://github.com/Bluemap-GTNH-Unofficial/BlueMap-GTNH/blob/main/NOTICE.md).

| Source | Copyright / author | Licence | What we used |
|---|---|---|---|
| [GTNewHorizons/ExampleMod1.7.10](https://github.com/GTNewHorizons/ExampleMod1.7.10) | Copyright (c) 2021 Johann Bernhardt | MIT | Template: build layout (`gradle.properties` keys, `settings.gradle.kts`, `.gitignore`). Its notice is in [LICENSE](LICENSE). |
| [BlueMap GTNH fork](https://github.com/Bluemap-GTNH-Unofficial/BlueMap) `legacy/.../ResourceKeys.kt` | Copyright (c) Blue (Lukas Rieger) and contributors | MIT | Code, compiled in unchanged with its MIT header (see `build.gradle.kts`), so the exporter and the map share one key normalisation. |
| [Gradle wrapper](https://github.com/gradle/gradle) | Copyright the original authors (Gradle) | Apache-2.0 | Unchanged `gradlew`, `gradlew.bat`, `gradle/wrapper/`. |
| [MyCTMLib](https://github.com/ABKQPO/MyCTMLib) | Copyright (c) 2024 初夏同学 | MIT | Format only: `TextureExporter.recordCtm` reads the `myctmlib` section of texture `.mcmeta` files. No MyCTMLib code. |
| [Prism Launcher](https://github.com/PrismLauncher/PrismLauncher) | Prism Launcher contributors | GPL-3.0 | Behaviour reference: `launcher/src/gtnh_client/profile.py` re-implements in Python the instance format rules it names (library rules, natives, class path order). |

## Compiled against (not redistributed)

The jar shades nothing; the GT New Horizons pack provides these at run time.

| Library | Licence | Used for |
|---|---|---|
| Minecraft 1.7.10 and Minecraft Forge | Mojang EULA / Forge licence | the mod itself |
| [Forgelin](https://github.com/GTNewHorizons/Forgelin) (Kotlin runtime) | LGPL-3.0 | Kotlin runtime |
| [GTNHLib](https://github.com/GTNewHorizons/GTNHLib) | LGPL-3.0 | quad capture (`TessellatorManager`) |
| [GT5-Unofficial](https://github.com/GTNewHorizons/GT5-Unofficial) | LGPL-3.0 | GregTech texture and machine data (`exporter/gt/`) |
| [EndlessIDs](https://github.com/FalsePattern/EndlessIDs), [ChunkAPI](https://github.com/FalsePattern/ChunkAPI) | see their repositories | development runs only |

## Textures

The exported resource pack contains textures of Minecraft and of the installed mods, which belong to their authors.
It is built on the user's machine from the user's own copy of the pack and is never published by this project.
The launcher downloads Mojang's client jar, libraries and assets from Mojang for local use only.
