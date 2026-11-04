# gtnh-client

Launches the GT New Horizons 2.9.0 **client** (Minecraft 1.7.10, lwjgl3ify, Java 25) on macOS arm64, Linux x64 and
Windows x64 without Prism Launcher, using plain `java` (see Java below). It also drives the one-shot BlueMap resource
export pipeline with the `bluemap-gtnh-exporter` client mod. In the dev container the clients run on the Windows host
instead, through `gtnh-client serve` (see Windows host and bridge).

Python ≥ 3.12, stdlib only. Paths default relative to the workspace (`WS`, the directory containing this
`gtnh-exporter/` checkout and its siblings such as `BlueMap/`; override with `BMGTNH_WORKSPACE`). Installed from the
release wheel instead of run from a checkout, `WS` is the current directory unless `BMGTNH_WORKSPACE` is set, and
`export` needs `--mod-jar`.

```sh
cd gtnh-exporter/launcher
uv run gtnh-client prepare --clean
uv run gtnh-client run --timeout 600
uv run gtnh-client export
uv run pytest
```

Java: `$BMGTNH_JAVA` (a java executable or a JDK directory); else `mise exec java@temurin-25.0.5+8.0.LTS -- java` when
mise is on PATH (the container, macOS); else the JDK in `~/.jdks/temurin-25.0.5+8` (the Windows host); else
`$JAVA_HOME`. A java other than Temurin 25.0.5+8 (read from the JDK's `release` file) is used with a warning on the
`[run] java ...` line: the oracle's reference captures depend on it.

## Commands

Every subcommand takes `--pack-version {2.9.0}`: the GTNH pack version of the default zip, client directory, server
and output paths. 2.9.0 is the only supported version. Without it the launcher uses the version the `--client-dir`
reports itself (`packs.client_version`: `S:ModPackVersion` in `.minecraft/config/GTNewHorizons/dreamcraft.cfg`), else
`$BMGTNH_PACK_VERSION`, else `2.9.0`. A `--pack-version` that differs from the client's own version stops the command
before anything runs (`gtnh-client: error: <dir> is a GTNH <own> client, not --pack-version <V>`, exit 1).
Per-version workspace directories end in `-<version>` (`WS/out/exporter-2.9.0`, `WS/worlds-2.9.0`,
`WS/out/oracle-2.9.0`). Below, `<V>` is the pack version.

### `prepare [--pack-version V] [--zip PATH] [--client-dir DIR] [--clean]`

- `--zip` Prism instance zip, default `WS/downloads/GT_New_Horizons_<V>_Java_17-26.zip`
  ([2.9.0](https://downloads.gtnewhorizons.com/Multi_mc_downloads/GT_New_Horizons_2.9.0_Java_17-26.zip); sha256 in
  `WS/downloads/gtnh-packs.json`).
- `--client-dir` default `WS/clients/gtnh-<V>` (`WS/clients/gtnh-2.9.0`).
- `--clean` deletes the client directory first.

Unpacks the zip with its top folder flattened (`DIR/.minecraft`, `DIR/patches`, `DIR/libraries`,
`DIR/mmc-pack.json`). Later zip entries overwrite earlier ones (`unzip -o` semantics; the zip has
case-only duplicate paths). The unpack is skipped when `DIR/.gtnh-client-unpacked.json` records the same
zip (path, size, mtime); use `--clean` for a fresh copy. Then it resolves the components in
`mmc-pack.json` order from `patches/<uid>.json` and downloads, SHA-1 verified, into the shared cache:

- the Mojang 1.7.10 `client.jar` (`mainJar`),
- the libraries of `net.minecraft`, `net.minecraftforge` and `org.lwjgl3` that are active for the host's classifier
  (`osx-arm64` on Apple silicon, `linux` on x86_64 Linux, `windows` on x86_64 Windows),
- asset index `1.7.10` and its 686 objects.

`natives` libraries are extracted into `DIR/natives`, honoring `extract.exclude`, and
`DIR/natives/.gtnh-client-natives.json` records the platform they are for: a client directory used from the container
and from the Windows host gets the natives of whichever runs it (re-extracted before a run when the record differs).
On `osx-arm64` only jinput's `natives-osx-arm64` qualifies and the LWJGL 3 `*-natives-macos-arm64` jars are ordinary
classpath entries; on x86_64 Linux it is jinput's `natives-linux`, and the LWJGL 3 `*-natives-linux` jars are on the
classpath; on x86_64 Windows jinput's and twitch's `natives-windows` are extracted and the LWJGL 3
`*-natives-windows`, `-windows-x86` and `-windows-arm64` jars are on the classpath (as Prism does; LWJGL picks its
architecture).

Rule semantics match Prism's `RuntimeContext` (`RuntimeContext.host()`). A rule's `os.name` matches the precise host
classifier (`osx-arm64` or `linux`). A bare `linux` or `osx` matches only on x86 hosts. So on Apple silicon the
`*-natives-macos` (x86_64) jars and `twitch-platform` (only an `osx` classifier) are inactive. A library that appears again later with a
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

Cleanup (all launching commands): SIGTERM/SIGHUP/Ctrl-C (on Windows Ctrl-C/Ctrl-Break, and for a launcher under
`gtnh-client serve` its stdin closing) kill the whole Java process tree and still restore `mods/`, temporary mods and
resource packs; an exporter jar left in `mods-disabled/` by a hard-killed launcher is moved back on the next run.

Display sleep (all launching commands, macOS only): while the client runs the launcher holds `caffeinate -dimsu -t
<timeout + 60 s>` and stops it when the client exits or the launcher is interrupted. With the display asleep the
client's GL loop is throttled so hard that Forge's login handshake times out even with `--connect-delay`. On Windows
the launcher only keeps the system from sleeping (`SetThreadExecutionState(ES_SYSTEM_REQUIRED)`) while clients run.

Headless display (Linux only): without `DISPLAY` the launcher starts a private Xvfb for each client and stops it with
the client; its output goes to `<client log>.xvfb.log`.

### `run [--client-dir DIR] [--mod JAR ...] [--jvm-arg ARG ...] [--xmx 10G] [--timeout 1800] [--username BlueMapExporter] [--log FILE]`

1. Runs `prepare` with the default zip if `DIR` does not exist. Otherwise it only fetches cache files
   that are missing.
2. Copies each `--mod` jar into `DIR/.minecraft/mods/`. For an exporter mod jar (any file name: recognised by its
   `ExporterMod` class) it first deletes every other exporter jar there; FML would load only one of them ("Duplicate
   mod found"), not necessarily the new one.
3. Writes the `window` category of `DIR/.minecraft/config/lwjgl3ify.cfg`, which is in Forge
   Configuration format. Other keys are kept. If the file does not exist, a minimal file is created
   and lwjgl3ify fills in the rest on boot.
   - `B:iconified=true`: the window starts minimized (`SDL_WINDOW_MINIMIZED`).
   - `B:focused=false`: the window does not take input focus.
4. Mutes the game: it sets `soundCategory_master:0.0` in `DIR/.minecraft/options.txt` and keeps all
   other options.
5. Launches from `cwd DIR/.minecraft` with:
   `<java> [-XstartOnFirstThread] -Xms<xmx> -Xmx<xmx> <+jvmArgs of me.eigenraven.lwjgl3ify.forgepatches> <--jvm-arg…> -Djava.library.path=DIR/natives -cp <libraries in component order>:<client.jar> com.gtnewhorizons.retrofuturabootstrap.MainStartOnFirstThread <minecraftArguments> --tweakClass cpw.mods.fml.common.launcher.FMLTweaker`
   (`<java>` as in Java above, e.g. `mise exec java@temurin-25.0.5+8.0.LTS -- java`).
   - The `minecraftArguments` are filled for offline play:
     - username: the `--username` value
     - uuid: `UUID.nameUUIDFromBytes("OfflinePlayer:<name>")`, written without dashes
     - accessToken `0`, userProperties `{}`, userType `legacy`, version `1.7.10`
     - gameDir: `DIR/.minecraft`
     - assetsDir: the cache's `assets/`
     - assetIndex: `1.7.10`
   - `-XstartOnFirstThread` comes from the `FirstThreadOnMacOS` trait and is passed only on macOS.
   - The local library `com.github.GTNewHorizons:lwjgl3ify:<version>:forgePatches` (MMC-hint `local`; 3.0.42 on
     2.9.0) is taken from `DIR/libraries/`.

stdout and stderr are merged and streamed to the console and to `--log`. The default log is
`DIR/logs-launcher/client-<YYYYmmdd-HHMMSS>.log`, starting with the cwd and the full command line.
The command exits with the java exit code. When `--timeout` expires, the whole process group
(the mise wrapper and java) gets SIGTERM, then SIGKILL after 15 s (on Windows the tree is killed with
`taskkill /T /F`), and the exit code is 124. Ctrl-C kills the group the same way and exits with 130; SIGTERM / SIGHUP
to the launcher exit with 143 / 129 (Windows: Ctrl-Break 149).

Values that start with `-` work as `--jvm-arg -Dfoo=bar` as well as `--jvm-arg=-Dfoo=bar`.

### `export [--pack-version V] [--client-dir DIR] [--clean] [--mod-jar JAR] [--out WS/out/exporter-<V>] [--extra-states FILE] [--extra-states-dump DUMP ...] [--resource-pack PACK ...] [--extra-mod JAR ...] [--pack-suffix S]`

1. Without `--mod-jar`, it builds the mod in `WS/gtnh-exporter` with
   `mise exec java@temurin-25.0.5+8.0.LTS -- ./gradlew -Porg.gradle.java.installations.paths=$(mise where java@zulu-8.98.0.107)/Contents/Home,$(mise where java@temurin-25.0.5+8.0.LTS)/Contents/Home build`.
   `/Contents/Home` is appended to the `mise where` directories only on macOS (JDK bundle layout).
   It takes the newest `build/libs/bluemap-gtnh-exporter-*.jar` that is not a `-dev` or `-sources` jar. Without mise
   (the Windows host) the build is refused: pass `--mod-jar` (through the bridge the caller builds it).
2. Runs `prepare`, honoring `--clean`.
3. Runs the client with defaults (`--xmx 10G --timeout 1800`) plus the mod and
   `-Dbluemap.gtnh.exporter.out=<abs out>`. With `--extra-states`, it also passes
   `-Dbluemap.gtnh.exporter.extraStates=<abs file>`; `--extra-states-dump` writes the non-vanilla states of each
   `bmh` dump to `<out>/extra-states.txt` and adds them to that list. `--resource-pack` and `--extra-mod` are enabled
   for this run only; `--pack-suffix` passes `-Dbluemap.gtnh.exporter.packSuffix`. The mod shuts the client down when
   it is done.
4. On exit code 0, it requires `<out>/export-report.json` and `<out>/bluemap-gtnh-resources-<version>[-<suffix>].zip` to exist
   and to be newer than the export start; `<version>` is the client's own `ModPackVersion` (the name the mod
   writes), else the pack version.
   It then prints the total wall time with the build/prepare/run split. Every failure exits non-zero.

### `cameras --jobs FILE [--pack-version V] [--client-dir DIR] [--parallel 3] [--resource-pack PACK ...] [--extra-mod JAR ...] [--mod-jar JAR] [--bmh-jar JAR] [--xmx 6G] [--timeout 3600]`

Reference screenshots of scene worlds for the world-scene oracle. `uv run e2e oracle world` (sibling `e2e/` checkout)
writes the jobs file and runs this command for the references its cache does not have, once per variant. No server
is involved: each client opens the scene worlds as single-player saves.

1. Reads the jobs file and checks every job before anything runs: `{"jobs": [{"scene", "world": <world snapshot dir
   with level.dat>, "cameras": <cameras.json>, "out": <dir>, "remove": [[x, y, z], ...], "freeze_random_ticks":
   true}]}`. Relative paths are relative to the file; `remove` defaults to the camera list's, `freeze_random_ticks` to
   true; two jobs may not share an `out`. The camera list (`cameras.json`, written by `e2e oracle world`) is `{"format":
   1, "scene", "world", "box", "cameras": [{"name", "eye": [x, y, z], "yaw", "pitch", "fov", "width", "height"}],
   "remove"}` with Minecraft angles in degrees (yaw 0 = south, 90 = west; pitch 90 = down) and the vertical FOV.
   `box` is the scene's dump box. `remove` lists cells set to air first (the e2e scene's `oracle.exclude`: block states
   whose client rendering throws, which fails their whole chunk's mesh).
2. Builds the exporter mod unless `--mod-jar`. The harness mod `bmh` (`--bmh-jar`, default the newest `bmh-*.jar` in
   `WS/servers/gtnh-<V>/mods/`, the jar the scene server runs) is installed into the clients for this run like an
   `--extra-mod`: it keeps the scene's frozen tile entities unticked and does the random-tick switch and the removals
   inside the integrated server.
3. Splits the jobs across `--parallel` workers (largest first onto the least loaded worker, cost 12 s per job plus
   2 s per camera; never more workers than jobs). Worker `i` is the instance `WS/clients/<client>-worker-<i>/` (see
   Layout): its `.minecraft` is mirrored from the `--client-dir` first, each job's world is copied into its
   `saves/oracle-<n>-<scene>/` (APFS clones on macOS, plain copies on Linux and Windows; `worlds-<V>` is never touched), and the
   stale outputs of the job's cameras in `out` are removed. `<worker>/cameras-spec.json` lists the worker's jobs for
   the mod.
4. For this run only (both files restored afterwards, in every worker): `options.txt` gets `ao:0`, `mipmapLevels:0`,
   `anisotropicFiltering:1`, `renderDistance:12`, `fboEnable:true`, `fancyGraphics:true`, `clouds:false`,
   `bobView:false`, `particles:2`, `anaglyph3d:false`, `pauseOnLostFocus:false`, `showInventoryAchievementHint:false`;
   Angelica's `config/angelica-options.json` gets `texture_filter_mode: NONE`, `texel_sampling: NEAREST` and
   `use_fog_occlusion: false` (created with just these when the client has none, e.g. a freshly prepared one, and
   removed again afterwards; otherwise Angelica samples textures with its RGSS/linear defaults).
5. Starts all worker clients at once (`--resource-pack` and `--extra-mod` installed for this run only, as with `export`)
   as `BlueMapCamera` with `-Dbluemap.gtnh.exporter.mode=cameras`, `-Dbluemap.gtnh.exporter.cameras=<spec>` and
   `-Dfml.queryResult=confirm`, and each worker with its own LWJGL native directory
   (`-Dorg.lwjgl.system.SharedLibraryExtractPath=<worker>/lwjgl-natives`: clients starting together on the shared
   default race on extracting `libSDL3.so` and fail to boot). Console lines carry a `[w<i>]` prefix; each worker logs
   to `<worker>/logs-launcher/cameras-<timestamp>.log`. The launcher samples each client's resident memory.
6. Deletes the saves, prints per worker the boot time, exit code and peak memory, per job the time split (open, join,
   cameras) and per camera the result and frame rate, and adds `launcher` (exit, peak RSS, log) to
   `<worker>/cameras-session.json`. Exit 0 only if every job's `cameras-report.json` is fresh, has no error, carries
   the current `CAPTURE_VERSION` and every camera's three files exist.

The mod's camera mode (`oracle/CameraCapture.kt`), one client session per worker: from the main menu it opens each job's
save as a single-player world. Before the player joins, the integrated server runs `bmh random-ticks off` (or `on` if
the job says so), `bmh set 0 x y z minecraft:air 0` per removed cell, `time set 6000`, `weather clear 1000000`,
`gamerule doDaylightCycle false` and `gamerule doMobSpawning false`. Its chunk radius is 8 chunks (`VIEW_DISTANCE`,
the dedicated scene servers' `view-distance`), so a camera sees the chunks it always saw with the 12-chunk client render
distance. The player gets creative flight; then per camera:

- the server loads the destination's chunk square and `tp`s the player there; the client pins position and rotation
  every tick and frame and sets the FOV at once;
- it waits until every chunk of the scene box in range and every chunk the server has sent the player is on the
  client, for 5 ticks (`loadTicks`). After 60 s (`cameraTimeoutSeconds`) the camera fails with the missing counts;
- it freezes the game: an invisible screen pauses the single-player game, so the world, entities, tile entities,
  particles and texture animations stop and the integrated server stops ticking. Partial ticks are 0 and the client's
  total world time is 0, so animated tile entity renderers stand at "frame 0";
- it waits until Angelica's chunk renderer reports no pending work (`isTerrainRenderComplete`, else vanilla's update
  list) and 3 frames at least 0.1 s apart differ in at most 0.05 % of the pixels, and captures the last of them. After
  30 s (`stableTimeoutSeconds`) the frame is captured anyway and marked not stable.

After the last camera it returns to the main menu (the integrated server saves and stops), waits until the integrated
server's thread has ended (up to 60 s) and opens the next save. The wait matters: `loadWorld(null)` returns once the
server reports itself stopped, while its thread still runs the mods' `FMLServerStoppedEvent` handlers; a save opened
meanwhile starts a server for which `MinecraftServer.getServer()` is already gone again (MineTweaker's
`FMLServerAboutToStartEvent` handler throws), FML's state machine errs, the server never starts and the client sat at
the main menu until `worldTimeoutSeconds` (300 s). That was the "flaky `--parallel`": it happened in serial runs too,
depending on timing.

Failures never wait silently: a job whose integrated server does not start or stops (a crash: BetterCrashes keeps the
client running) fails at once with that reason and the next job starts. So does a job during which the client writes a
crash report (`crash-reports/`, checked every second): BetterCrashes resets the game and lets the client go on, but a
block whose rendering throws fails its whole chunk mesh, and Angelica's renderer may not survive the reset (it then
throws every frame and no frame is drawn at all); the job's error is the report's description and exception, and for a
block the block, meta and location to put into the scene's `oracle.exclude`. While a world is joined or a server thread
ends, a line every 30 s shows what is awaited. Minecraft's timer drops the time of frames longer than 1 s (no client
tick runs at all), so with such slow frames the capture steps on frames instead. A watchdog thread logs the client and
server thread stacks every 30 s while the capture has not stepped (the client thread is stuck in a call or in a single
frame) and after `worldTimeoutSeconds` writes the session report (its `error` names the phase and job; the launcher
prints it with the job's failure) and kills the client: SIGKILL, on Windows `taskkill /F` (a client stuck in a GL call
ends neither with `System.exit` nor with `Runtime.halt`, and FML traps both when a mod calls them).

Capture conditions: game framebuffer resized to the camera size (the minimized window is not touched), no GUI, hand or
block outline; flat lighting with a white lightmap (full brightness; vanilla's face shade 1.0 up, 0.5 down, 0.8
north/south, 0.6 west/east stays); fog pushed beyond the far plane; FOV exactly the camera's (the flying FOV boost
cancelled); block atlas animations pinned to their first frame; particles cleared every frame.

Outputs per job, in its `out`: `<view>.png`; `<view>.depth.bin` (float32 little endian, rows top to bottom, OpenGL
window depth 0..1, 1 = nothing drawn); `<view>.json` with the projection and modelview matrices (column-major, read at
`RenderWorldLastEvent`; world to eye is `modelview * translate(-camera)`), the camera position, the load and stability
record, frames and frame rate; `cameras-report.json` (`scene`, `capture_version`, `cameras`, `captured`, `failed`,
`error`, `seconds`, `timings` {open, join, cameras, leave}, `settings`, `results[]` {name, captured, error, settled,
stable, seconds, loadSeconds, fps}). Per worker: `<worker>/cameras-session.json` (boot time, per-job results).

`CAPTURE_VERSION` in `gtnh_client/cameras.py` versions all of this (the e2e reference cache keys on it): bump it when
anything that can change a capture changes.

### `states [--pack-version V] [--variant default] [--out DIR] [--states-from PACK] [--only REGEX] [--client-dir DIR] [--resource-pack PACK ...] [--extra-mod JAR ...] [--mod-jar JAR] [--parallel 1] [--xmx 10G] [--timeout 3600] [--log FILE]`

Reference renders of every block state for the per-state rendering oracle (`uv run e2e oracle states` in the
sibling `e2e/` checkout compares them with BlueMap; the e2e README has the whole procedure).

1. Builds the exporter mod unless `--mod-jar`; prepares the client directory if it does not exist.
2. Writes the state list `WS/out/oracle-<V>/<variant>/states/states.txt` (`<modid:name> <meta>` lines): the vanilla
   states that Mojang's 1.13 flattening table knows (`registered = 1` in the BlueMap fork's
   `legacy/.../flattening_1_7_10.tsv`, air excluded), then every state of the exported pack's `gtnh/blocks.ndjson`
   (`--states-from`, default `WS/out/exporter-<V>/bluemap-gtnh-resources-<V>.zip`: the exporter's own state
   discovery, whatever the export result). `--only` keeps the states whose `<modid:name> <meta>` matches and writes
   `states-only.txt` instead; without it the output directory is emptied first.
3. Runs the client (`--resource-pack` and `--extra-mod` installed for this run only, as with `export`) with
   `-Dbluemap.gtnh.exporter.mode=states`, `-Dbluemap.gtnh.exporter.states=<list>` and
   `-Dbluemap.gtnh.exporter.out=<out>` (default `WS/out/oracle-<V>/<variant>/states/client/`). The mod draws the states
   at the main menu and quits (see the exporter README, "Per-state oracle").
   With `--parallel N` (N > 1) the list is cut into N contiguous shards of whole 64-state batches (the mod's
   framebuffer batch, so every state is drawn in the same batch as in a single run); worker `i`
   (`WS/clients/<client>-worker-<i>/`, see Layout) draws its shard straight into `<out>` and writes its index to
   `<worker>/states-index.json` (`-Dbluemap.gtnh.exporter.statesIndex`); the launcher merges the indexes in shard order
   into `<out>/index.json` (`seconds` = the slowest shard). The workers' LWJGL natives are separate as with `cameras`.
   The default stays 1: on Linux (llvmpipe) three workers drew the 62,659 states in 26 s instead of 54 s, but three
   concurrent boots took about 95 s instead of 65 s, so the run took 120 s against 121 s serially.
4. Requires `<out>/index.json` newer than the run with one entry and one image per listed state; prints the
   counts of states with render errors, blank states and states with a tile-entity special renderer.
   `STATES_CAPTURE_VERSION` in `gtnh_client/states.py` versions the per-state capture for the e2e reference cache.

Variants: `default` as above; `modernity` with `--client-dir WS/clients/gtnh-modernity-<V> --resource-pack
WS/downloads/modernity/Modernity-2.9.x-<sha>.zip --extra-mod WS/downloads/modernity/MyCTMLib-<v>-29x.jar --variant
modernity`; `addons` with `--client-dir WS/clients/gtnh-addons-<V> --states-from
WS/out/exporter-addons-<V>/<addons zip> --extra-mod <addon jar>... --variant addons`. On 2.9.0 the default variant's
62,659 states take 67 s (32 s drawing) on the MacBook, 121 s (54 s drawing) in the Linux container on llvmpipe, 94 s
(49 s drawing) on the Windows host (RTX 5090).

## Windows host and bridge

The dev container runs the pipeline; its clients run natively on the Windows host (GPU OpenGL instead of llvmpipe on
the CPU). Setup on the host, once:

- uv on PATH. The launcher's environment there is `.venv-windows` (`UV_PROJECT_ENVIRONMENT=.venv-windows`, gitignored):
  `.venv` belongs to the container, and `uv run` on Windows deletes a Linux `.venv` it finds.
- Temurin 25.0.5+8, the container's `java@temurin-25.0.5+8.0.LTS`: Adoptium's
  [`OpenJDK25U-jdk_x64_windows_hotspot_25.0.5_8.zip`](https://github.com/adoptium/temurin25-binaries/releases/tag/jdk-25.0.5%2B8)
  (sha256 `636bdc13dc649ddc7d5921c5c7d23cca6e44fd19d662b45cc9c02135a6c96c42`) unpacked into
  `%USERPROFILE%\.jdks\temurin-25.0.5+8`, where the launcher finds it (or `BMGTNH_JAVA`); the `java` on PATH is not used.

Run natively (PowerShell, `WS` the Windows checkout): `$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"`, then
`uv run gtnh-client prepare|run|export|cameras|states ...` in `gtnh-exporter\launcher` as anywhere else. What differs:
there is no mise, so `export`, `cameras` and `states` need `--mod-jar` (the build stays in the container); a client tree
is its own process group without a console window (`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`) and is killed with
`taskkill /T /F`; resident memory is the working set of the tree; there is no Xvfb. The window stays minimized and
unfocused (lwjgl3ify's `iconified`/`focused`): measured with the foreground window sampled every 0.25 s during a 300 s
`run` and a three-worker `cameras` run, the user's window kept the focus and every client window stayed minimized,
and the minimized clients' captures match the container's (camera views: 0.1-0.7 % of the pixels differ by more than 8
in a channel, depth to 0.01).

The bridge: `gtnh-client serve [--port 8390] [--log WS/out/gtnh-client-serve.log]` on the host listens on 127.0.0.1
only (`host.wslc.internal` reaches the host's loopback from a wslc container). `windows\serve.ps1` starts it hidden
and detached (`-Stop`, `-Install` for a logon task in the interactive session, which the clients' OpenGL needs,
`-Uninstall`; the process's own output goes to `WS\out\gtnh-client-serve.console.log`). With
`BMGTNH_CLIENT_HOST=http://host.wslc.internal:8390` (set by `.devcontainer/run.sh` and `devcontainer.json`), every
`prepare`, `run`, `play`, `export`, `cameras` and `states` in the container is forwarded (`gtnh_client.bridge`):

1. The caller does what needs the container first: without `--mod-jar` it builds the exporter mod and passes the jar;
   `cameras` without `--bmh-jar` gets the scene server's jar.
2. It checks every path argument and the paths in a `cameras --jobs` file: each must lie in the workspace (`/ws`) and
   not on a container volume over it (`/proc/self/mountinfo`), since the host sees only the bind-mounted files. A file
   on a volume (the scene server's bmh jar, the camera lists e2e writes into `out/oracle-<V>/`) is sent along and
   staged on the host; a world or an output there, or any path outside `/ws`, fails at once.
3. It sends argv, cwd, its workspace root and its `BMGTNH_*` variables (not `BMGTNH_JAVA`). The server maps `/ws/...`
   to the Windows checkout in argv (whole tokens and after `=`, e.g. `--jvm-arg=-Dkey=/ws/x`), cwd and the variables,
   writes a copy of a `cameras --jobs` file with host paths (and the uploaded camera lists) for the launcher, and runs
   `gtnh-client <argv>` there.
   An absolute path outside `/ws` is refused.
4. The output (stdout and stderr merged) streams back line by line, Windows paths under the checkout written as
   `/ws/...`, always as UTF-8: the client JVM prints UTF-8 (`-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8`, which
   the launcher passes everywhere; Java's default is the code page, GBK on a Chinese Windows, and mise aborts a task
   on output that is not UTF-8), and a line that still is not (native code) is read in the host's code page, else
   with U+FFFD. The caller exits with the command's exit code (codes above 255, such as a Windows crash status, as 1).
5. When the caller goes away (killed, a task timeout, Ctrl-C: the connection closes) the server closes the launcher's
   stdin; the launcher stops as on SIGTERM (client trees killed, mods, packs and options restored, exit 143) and is
   killed with its tree after 90 s if it has not. Measured: a caller killed 44 s into a `run` left no java behind 1 s
   later. Heartbeats every 5 s keep the stream alive; a caller that hears nothing for 120 s gives up.
6. No server: the command fails within the 5 s connect timeout, naming the start script.

The server log (`WS/out/gtnh-client-serve.log`) records each request (argv, launcher pid, cwd), its exit code and time,
refusals and disconnects. Bridge code changes need a server restart; launcher code is read afresh by every command.
`play --server` addresses are resolved on the host.

## Layout

```
WS/downloads/mc-cache/                 shared cache, survives `--clean`
  libraries/<maven path>               all remote libraries + com/mojang/minecraft/1.7.10/minecraft-1.7.10-client.jar
  assets/indexes/1.7.10.json
  assets/objects/<hh>/<hash>
WS/clients/gtnh-2.9.0/                 client dir (unpacked instance)
  .minecraft/                          game dir (mods, config, logs, saves)
  libraries/lwjgl3ify-3.0.42-forgePatches.jar
  patches/*.json, mmc-pack.json
  natives/                             extracted native libs (jinput; twitch on Windows)
    .gtnh-client-natives.json          the platform they were extracted for
  logs-launcher/client-*.log
  .gtnh-client-unpacked.json           unpack marker
WS/clients/gtnh-2.9.0-worker-<i>/      worker instance of `cameras`/`states --parallel` (any base client: <client>-worker-<i>)
  .minecraft/                          own game dir, mirrored from the base's before every run except its saves/, logs/,
                                       crash-reports/, screenshots/, backups/, mods-disabled/
  logs-launcher/cameras-*.log          worker logs
  cameras-spec.json, cameras-session.json, states-shard.txt, states-index.json
  lwjgl-natives/                       the worker's own LWJGL native libraries (extracted at boot)
WS/locks/client.lock                   client lock (see below)
```

A worker runs the base client's pack (patches, instance libraries and natives of the base directory, libraries and
assets from the shared cache) with its own game directory. The mirror compares size and modification time, removes
what the base no longer has and copies only what differs. On macOS the copies are APFS clones (`clonefile`, copy on
write), so a worker costs almost no disk and a sync takes about a second. On Linux they are plain copies: about 1 GB
per worker, the first sync of a new worker takes about 40 s (ext4, container volume), later ones only copy what
changed. The mods' boot-time config rewrites in a worker are undone by the next sync.

Downloads are written to a `.part` file, checked against SHA-1 and size, and renamed into place. 16
downloads run in parallel. `prepare` re-hashes cached files, which takes about 0.1 s for all of them.

Client lock: `prepare`, `run`, `play`, `export`, `cameras` and `states` hold `WS/locks/client.lock` (`gtnh_client.lock`;
`flock`, on Windows `msvcrt.locking` on a byte beyond the holder record) for their whole run, one hold for all workers
of a parallel run. The holder record (pid, host, command, start, cwd) is in the file; a second command prints it and
waits, `BMGTNH_CLIENT_LOCK_WAIT=<seconds>` bounds the wait (`0`: fail at once). Through the bridge the lock is the
host's (`gtnh-client serve` runs each command as a launcher there), so callers queue there.

## Measured timings (MacBook arm64, 18 cores, 64 GB, GTNH 2.9.0)

| Step | Time |
|---|---|
| `prepare` on an unpacked dir, warm cache (67 libraries + client jar + index 60.3 MB, 674 asset objects 112.2 MB) | 0.1 s |
| Boots to main menu | 31 s and 44 s (`The game loaded in 31.257 seconds`, `43.669 seconds`) |
| `export` with the `gtnh-zoo` dump | 53 s (build 2 s, prepare 0 s, client run 51 s): 4,087 blocks, 61,468 states from 138 mods |

Linux container (32 cores, 62 GB visible, Mesa on Xvfb), `cameras` with the 8 default-variant scenes (81 cameras):

| Run | Client time (boot to last exit) | Notes |
|---|---|---|
| `--parallel 1`, llvmpipe, `--xmx 10G` | 300 s (boot 57 s) | peak RSS 13.6 GiB |
| `--parallel 3`, llvmpipe, `--xmx 6G` | 192 s (boots 71-77 s) | peak RSS 8.9-9.2 GiB per worker; worker 1 has endgame alone (118 s), which bounds larger N |
| `--parallel 1`, d3d12 (GPU) | did not finish | frames of gtnh-zoo slowed to minutes after four earlier jobs; d3d12 captures also differ run to run |

Captures are not bit-identical between runs: serial vs `--parallel 3` on llvmpipe, 41 of 80 cameras are identical, the
others differ by 1 in a channel or in small regions (gtnh-zoo origin-perspective: 22 % of the pixels); two serial d3d12
runs differ in the same places (gtnh-zoo origin-perspective: 23 %).

Windows host (Ryzen 9 9950X3D, 32 threads, 96 GB, RTX 5090 with NVIDIA 617.42 OpenGL 4.6, Temurin 25.0.5+8), measured
2026-11-03:

| Step | Time | Container (llvmpipe) |
|---|---|---|
| `prepare` from the zip (17,775 files unpacked, 42 Windows libraries downloaded) | 13.5 s | |
| Boots to main menu (`The game loaded in`) | 41-59 s alone, 67-74 s three at once | 57-90 s |
| `export` with the `gtnh-zoo` dump | 69 s | 90-110 s |
| `states`, default variant (62,659 states) | 94 s, drawing 49 s (a second run 108 s / 64 s) | 121 s / 54 s |
| `cameras`, 8 scenes / 81 cameras, `--parallel 1` | 235 s (client 225 s, boot 49 s) | 258 s |
| `cameras`, `--parallel 3` | 183 s (setup 15 s, clients 164 s); 266 s with new workers (first copies 103 s) | 177-195 s |

Clients are bound by mod loading on the CPU, not by the GPU: a world capture spends most of its time loading chunks
and waiting for stable frames. Peak working set 7.9-8.4 GiB per client. Windows commits a JVM's whole `-Xms` heap at
start: with the WSL VM holding 30 GB, a third 6 GB worker once failed with "Native memory allocation (mmap) failed to
map 6442450944 bytes" (commit limit 101.7 GB = RAM + page file, 26 GB free); the free commit
(`Get-CimInstance Win32_OperatingSystem`, `FreeVirtualMemory`) must cover the clients.

Renderer differences (same inputs): block states drawn by NVIDIA and by llvmpipe are byte-identical for 76.6 % of the
colour images and 93.6 % of the face images; 7.4 % / 6.4 % have pixels more than 8 off in a channel. Two NVIDIA runs
agree on 99.7 % (the rest are blocks with random textures). Camera views differ in 0.1-0.7 % of the pixels by more
than 8, depth by at most 0.01. The exported resource pack is the same (all 42,871 entries' textures; 7 JSON files of
blocks with random textures differ run to run anyway).

Boot milestones in the log:

- `[FML]: Forge Mod Loader has successfully loaded 324 mods` (on 2.9.0) marks the end of mod loading.
- `[ArchaicFix]: The game loaded in N seconds.` / `[Angelica]: The game loaded in N seconds.` mark
  the main menu. They come right after `[betterloadingscreen]: BLS Splash loading thread closing`.

## Tests

`uv run pytest` covers:

- os/arch rule evaluation,
- native classifier resolution and classpath order, from the real patches read out of the instance zip
  (these tests are skipped when the zip is absent),
- the offline UUID, checked against Java's `UUID.nameUUIDFromBytes`,
- `cameras`: camera list and jobs file validation (paths, defaults, bad jobs), the saves (copies, stale ones removed,
  snapshot untouched), the spec, the capture settings (written and restored, also on errors), the output checks and
  the parser,
- `workers`: job splitting, the worker layout and the game directory mirror,
- `states`: the state list (vanilla from the flattening table, then the pack's `gtnh/blocks.ndjson`, `--only`), the
  shards, the index merge and the output checks,
- `lock`: the client lock,
- processes (`test_process.py`): a client tree killed whole (also on timeout, exit 124), its memory, the terminating
  signals, the Java choice and the natives' platform record,
- the bridge (`test_bridge.py`): path mapping both ways, the volume and workspace checks, uploads, and a server
  running a fake launcher: output streamed (GBK and invalid bytes arrive as valid UTF-8), exit code returned,
  refusals, no server, and a caller that goes away stopping the launcher through its SIGTERM cleanup.

The suite runs in the container and natively on Windows (`$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"; uv run
pytest`; the tests that drive fake POSIX tools are skipped there).
