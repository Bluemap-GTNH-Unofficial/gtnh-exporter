# gtnh-client

Launches the GT New Horizons 2.9.0-RC-2 **client** (Minecraft 1.7.10, lwjgl3ify, Java 25) on macOS arm64
without Prism Launcher, using plain `java` from `mise exec java@temurin-25.0.5+8.0.LTS`. It also drives
the one-shot BlueMap resource export pipeline with the `bluemap-gtnh-exporter` client mod.

Python ≥ 3.12, stdlib only. Paths default relative to the workspace (`WS`, the directory containing
`BlueMap-GTNH/`; override with `BMGTNH_WORKSPACE`).

```sh
cd BlueMap-GTNH/tools/gtnh-exporter/launcher
uv run gtnh-client prepare --clean
uv run gtnh-client run --timeout 600
uv run gtnh-client export
uv run pytest
```

## Commands

### `prepare [--zip PATH] [--client-dir DIR] [--clean]`

- `--zip` Prism instance zip, default `WS/downloads/GT_New_Horizons_2.9.0-RC-2_Java_17-26.zip`.
- `--client-dir` default `WS/clients/gtnh-2.9.0-RC-2`.
- `--clean` deletes the client directory first.

Unpacks the zip with its top folder flattened (`DIR/.minecraft`, `DIR/patches`, `DIR/libraries`,
`DIR/mmc-pack.json`). Later zip entries overwrite earlier ones (`unzip -o` semantics; the zip has
case-only duplicate paths). The unpack is skipped when `DIR/.gtnh-client-unpacked.json` records the same
zip (path, size, mtime); use `--clean` for a fresh copy. Then it resolves the components in
`mmc-pack.json` order from `patches/<uid>.json` and downloads, SHA-1 verified, into the shared cache:

- the Mojang 1.7.10 `client.jar` (`mainJar`),
- the libraries of `net.minecraft`, `net.minecraftforge` and `org.lwjgl3` that are active for `osx-arm64`,
- asset index `1.7.10` and its 686 objects.

`natives` libraries are extracted into `DIR/natives`, honoring `extract.exclude`. Only jinput's
`natives-osx-arm64` qualifies. The LWJGL 3 `*-natives-macos-arm64` jars are ordinary classpath entries.

Rule semantics match Prism's `RuntimeContext`. A rule's `os.name` matches the precise `osx-arm64`
classifier. A bare `osx` matches only on x86 hosts. So `*-natives-macos` (x86_64) jars and
`twitch-platform` (only an `osx` classifier) are inactive. A library that appears again later with a
higher version replaces the earlier one in place: forge's guava 17.0 takes the slot of Minecraft's
guava 15.0.

### `play [--client-dir DIR] [--server HOST[:PORT]] [--connect-delay 2] [--username BlueMapPlayer] [--jvm-arg ARG ...] [--xmx 10G] [--timeout 1800] [--log FILE]`

Launches the client like `run` (minimized, muted) but without the exporter mod: `bluemap-gtnh-exporter*.jar` is moved
to `.minecraft/mods-disabled/` for the session and moved back afterwards. `--server` appends
`--server <host> --port <port>` (default port 25565) to the game arguments, so 1.7.10 connects on start, e.g.
`uv run gtnh-client play --server 127.0.0.1:25567 --username Tester` against an `online-mode=false` server.

`--connect-delay` (default 2 s, 0 = connect directly) routes the connection through a local proxy
(`gtnh_client.proxy`) that holds the server's first reply: Forge 1.7.10 opens `GuiConnecting` before it creates the
client latch, so a fast local server's LoginSuccess is lost and the FML handshake times out ("Timeout waiting for
client thread to catch up!").

Cleanup (all launching commands): SIGTERM/SIGHUP/Ctrl-C kill the whole Java process tree and still restore
`mods/`, temporary mods and resource packs; an exporter jar left in `mods-disabled/` by a hard-killed launcher is moved
back on the next run.

### `run [--client-dir DIR] [--mod JAR ...] [--jvm-arg ARG ...] [--xmx 10G] [--timeout 1800] [--username BlueMapExporter] [--log FILE]`

1. Runs `prepare` with the default zip if `DIR` does not exist. Otherwise it only fetches cache files
   that are missing.
2. Copies each `--mod` jar into `DIR/.minecraft/mods/`. For `bluemap-gtnh-exporter*.jar` it first
   deletes the older `bluemap-gtnh-exporter*.jar` files there.
3. Writes the `window` category of `DIR/.minecraft/config/lwjgl3ify.cfg`, which is in Forge
   Configuration format. Other keys are kept. If the file does not exist, a minimal file is created
   and lwjgl3ify fills in the rest on boot.
   - `B:iconified=true`: the window starts minimized (`SDL_WINDOW_MINIMIZED`).
   - `B:focused=false`: the window does not take input focus.
4. Mutes the game: it sets `soundCategory_master:0.0` in `DIR/.minecraft/options.txt` and keeps all
   other options.
5. Launches from `cwd DIR/.minecraft` with:
   `mise exec java@temurin-25.0.5+8.0.LTS -- java -XstartOnFirstThread -Xms<xmx> -Xmx<xmx> <+jvmArgs of me.eigenraven.lwjgl3ify.forgepatches> <--jvm-arg…> -Djava.library.path=DIR/natives -cp <libraries in component order>:<client.jar> com.gtnewhorizons.retrofuturabootstrap.MainStartOnFirstThread <minecraftArguments> --tweakClass cpw.mods.fml.common.launcher.FMLTweaker`.
   - The `minecraftArguments` are filled for offline play:
     - username: the `--username` value
     - uuid: `UUID.nameUUIDFromBytes("OfflinePlayer:<name>")`, written without dashes
     - accessToken `0`, userProperties `{}`, userType `legacy`, version `1.7.10`
     - gameDir: `DIR/.minecraft`
     - assetsDir: the cache's `assets/`
     - assetIndex: `1.7.10`
   - `-XstartOnFirstThread` comes from the `FirstThreadOnMacOS` trait.
   - The local library `com.github.GTNewHorizons:lwjgl3ify:3.0.37:forgePatches` (MMC-hint `local`) is
     taken from `DIR/libraries/`.

stdout and stderr are merged and streamed to the console and to `--log`. The default log is
`DIR/logs-launcher/client-<YYYYmmdd-HHMMSS>.log`, starting with the cwd and the full command line.
The command exits with the java exit code. When `--timeout` expires, the whole process group
(the mise wrapper and java) gets SIGTERM, then SIGKILL after 15 s, and the exit code is 124.
Ctrl-C kills the group the same way and exits with 130; SIGTERM / SIGHUP to the launcher exit with 143 / 129.

Values that start with `-` work as `--jvm-arg -Dfoo=bar` as well as `--jvm-arg=-Dfoo=bar`.

### `export [--client-dir DIR] [--clean] [--mod-jar JAR] [--out WS/out/exporter] [--extra-states FILE]`

1. Without `--mod-jar`, it builds the mod in `WS/BlueMap-GTNH/tools/gtnh-exporter` with
   `mise exec java@temurin-25.0.5+8.0.LTS -- ./gradlew -Porg.gradle.java.installations.paths=$(mise where java@zulu-8.98.0.107)/Contents/Home,$(mise where java@temurin-25.0.5+8.0.LTS)/Contents/Home build`.
   It takes the newest `build/libs/bluemap-gtnh-exporter-*.jar` that is not a `-dev` or `-sources` jar.
2. Runs `prepare`, honoring `--clean`.
3. Runs the client with defaults (`--xmx 10G --timeout 1800`) plus the mod and
   `-Dbluemap.gtnh.exporter.out=<abs out>`. With `--extra-states`, it also passes
   `-Dbluemap.gtnh.exporter.extraStates=<abs file>`. The mod shuts the client down when it is done.
4. On exit code 0, it requires `<out>/export-report.json` and
   `<out>/bluemap-gtnh-resources-2.9.0-RC-2.zip` to exist and to be newer than the export start.
   It then prints the total wall time with the build/prepare/run split. Every failure exits non-zero.

## Layout

```
WS/downloads/mc-cache/                 shared cache, survives `--clean`
  libraries/<maven path>               all remote libraries + com/mojang/minecraft/1.7.10/minecraft-1.7.10-client.jar
  assets/indexes/1.7.10.json
  assets/objects/<hh>/<hash>
WS/clients/gtnh-2.9.0-RC-2/            client dir (unpacked instance)
  .minecraft/                          game dir (mods, config, logs, saves)
  libraries/lwjgl3ify-3.0.37-forgePatches.jar
  patches/*.json, mmc-pack.json
  natives/                             extracted native libs (jinput)
  logs-launcher/client-*.log
  .gtnh-client-unpacked.json           unpack marker
```

Downloads are written to a `.part` file, checked against SHA-1 and size, and renamed into place. 16
downloads run in parallel. `prepare` re-hashes cached files, which takes about 0.1 s for all of them.

## Measured timings (MacBook arm64, 18 cores, 64 GB)

| Step | Time |
|---|---|
| `prepare --clean`, empty cache | 16 s: unpack 16146 files 3.1 s; 67 libraries + client jar + index (60.3 MB) 3.1 s; 674 asset objects (112.2 MB) 9.2 s. 172.5 MB downloaded in total |
| `prepare --clean`, warm cache | 3.9 s (mostly the unpack) |
| `prepare` on an unpacked dir | 0.1 s |
| First boot after `prepare --clean`, to main menu | ~96 s (`The game loaded in 95.622 seconds`) |
| Later boots, to main menu | ~33 s (`The game loaded in 32.772 seconds`) |

Boot milestones in the log:

- `[FML]: Forge Mod Loader has successfully loaded 322 mods` marks the end of mod loading.
- `[ArchaicFix]: The game loaded in N seconds.` / `[Angelica]: The game loaded in N seconds.` mark
  the main menu. They come right after `[betterloadingscreen]: BLS Splash loading thread closing`.

## Tests

`uv run pytest` covers:

- os/arch rule evaluation,
- native classifier resolution and classpath order, from the real patches read out of the instance zip
  (these tests are skipped when the zip is absent),
- the offline UUID, checked against Java's `UUID.nameUUIDFromBytes`.
