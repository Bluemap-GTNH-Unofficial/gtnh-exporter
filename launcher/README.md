# gtnh-client

Launches the GT New Horizons 2.9.0 **client** (Minecraft 1.7.10, lwjgl3ify, Java 25) on macOS arm64
without Prism Launcher, using plain `java` from `mise exec java@temurin-25.0.5+8.0.LTS`. It also drives
the one-shot BlueMap resource export pipeline with the `bluemap-gtnh-exporter` client mod.

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

Display sleep (all launching commands, macOS only): while the client runs the launcher holds `caffeinate -dimsu -t
<timeout + 60 s>` and stops it when the client exits or the launcher is interrupted. With the display asleep the
client's GL loop is throttled so hard that Forge's login handshake times out even with `--connect-delay`.

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
   - The local library `com.github.GTNewHorizons:lwjgl3ify:<version>:forgePatches` (MMC-hint `local`; 3.0.42 on
     2.9.0) is taken from `DIR/libraries/`.

stdout and stderr are merged and streamed to the console and to `--log`. The default log is
`DIR/logs-launcher/client-<YYYYmmdd-HHMMSS>.log`, starting with the cwd and the full command line.
The command exits with the java exit code. When `--timeout` expires, the whole process group
(the mise wrapper and java) gets SIGTERM, then SIGKILL after 15 s, and the exit code is 124.
Ctrl-C kills the group the same way and exits with 130; SIGTERM / SIGHUP to the launcher exit with 143 / 129.

Values that start with `-` work as `--jvm-arg -Dfoo=bar` as well as `--jvm-arg=-Dfoo=bar`.

### `export [--pack-version V] [--client-dir DIR] [--clean] [--mod-jar JAR] [--out WS/out/exporter-<V>] [--extra-states FILE] [--extra-states-dump DUMP ...] [--resource-pack PACK ...] [--extra-mod JAR ...] [--pack-suffix S]`

1. Without `--mod-jar`, it builds the mod in `WS/gtnh-exporter` with
   `mise exec java@temurin-25.0.5+8.0.LTS -- ./gradlew -Porg.gradle.java.installations.paths=$(mise where java@zulu-8.98.0.107)/Contents/Home,$(mise where java@temurin-25.0.5+8.0.LTS)/Contents/Home build`.
   It takes the newest `build/libs/bluemap-gtnh-exporter-*.jar` that is not a `-dev` or `-sources` jar.
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

### `cameras --jobs FILE [--pack-version V] [--client-dir DIR] [--parallel 2] [--resource-pack PACK ...] [--extra-mod JAR ...] [--mod-jar JAR] [--bmh-jar JAR] [--xmx 10G] [--timeout 3600]`

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
   `saves/oracle-<n>-<scene>/` (APFS clones; `worlds-<V>` is never touched), and the stale outputs of the job's cameras
   in `out` are removed. `<worker>/cameras-spec.json` lists the worker's jobs for the mod.
4. For this run only (both files restored afterwards, in every worker): `options.txt` gets `ao:0`, `mipmapLevels:0`,
   `anisotropicFiltering:1`, `renderDistance:12`, `fboEnable:true`, `fancyGraphics:true`, `clouds:false`,
   `bobView:false`, `particles:2`, `anaglyph3d:false`, `pauseOnLostFocus:false`, `showInventoryAchievementHint:false`;
   Angelica's `config/angelica-options.json` gets `texture_filter_mode: NONE`, `texel_sampling: NEAREST` and
   `use_fog_occlusion: false`.
5. Starts all worker clients at once (`--resource-pack` and `--extra-mod` installed for this run only, as with `export`)
   as `BlueMapCamera` with `-Dbluemap.gtnh.exporter.mode=cameras`, `-Dbluemap.gtnh.exporter.cameras=<spec>` and
   `-Dfml.queryResult=confirm`. Console lines carry a `[w<i>]` prefix; each worker logs to
   `<worker>/logs-launcher/cameras-<timestamp>.log`. The launcher samples each client's resident memory.
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

After the last camera it returns to the main menu (the integrated server saves and stops) and opens the next save.
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
   into `<out>/index.json` (`seconds` = the slowest shard).
4. Requires `<out>/index.json` newer than the run with one entry and one image per listed state; prints the
   counts of states with render errors, blank states and states with a tile-entity special renderer.
   `STATES_CAPTURE_VERSION` in `gtnh_client/states.py` versions the per-state capture for the e2e reference cache.

Variants: `default` as above; `modernity` with `--client-dir WS/clients/gtnh-modernity-<V> --resource-pack
WS/downloads/modernity/Modernity-2.9.x-<sha>.zip --extra-mod WS/downloads/modernity/MyCTMLib-<v>-29x.jar --variant
modernity`; `addons` with `--client-dir WS/clients/gtnh-addons-<V> --states-from
WS/out/exporter-addons-<V>/<addons zip> --extra-mod <addon jar>... --variant addons`. On 2.9.0 the default variant's
62,659 states take 67 s (32 s drawing).

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
  natives/                             extracted native libs (jinput)
  logs-launcher/client-*.log
  .gtnh-client-unpacked.json           unpack marker
WS/clients/gtnh-2.9.0-worker-<i>/      worker instance of `cameras`/`states --parallel` (any base client: <client>-worker-<i>)
  .minecraft/                          own game dir, mirrored from the base's before every run except its saves/, logs/,
                                       crash-reports/, screenshots/, backups/, mods-disabled/
  logs-launcher/cameras-*.log          worker logs
  cameras-spec.json, cameras-session.json, states-shard.txt, states-index.json
WS/locks/client.lock                   client lock (see below)
```

A worker runs the base client's pack (patches, instance libraries and natives of the base directory, libraries and
assets from the shared cache) with its own game directory. The mirror compares size and modification time, removes
what the base no longer has and copies with APFS clones (`clonefile`, copy on write), so a worker costs almost no disk
and a sync takes about a second; the mods' boot-time config rewrites in a worker are undone by the next sync.

Downloads are written to a `.part` file, checked against SHA-1 and size, and renamed into place. 16
downloads run in parallel. `prepare` re-hashes cached files, which takes about 0.1 s for all of them.

Client lock: `run`, `play`, `export`, `cameras` and `states` hold `WS/locks/client.lock` (`gtnh_client.lock`, `flock`)
for their whole run, one hold for all workers of a parallel run. The holder record (pid, host, command, start, cwd) is
in the file; a second command prints it and waits, `BMGTNH_CLIENT_LOCK_WAIT=<seconds>` bounds the wait (`0`: fail at
once).

## Measured timings (MacBook arm64, 18 cores, 64 GB, GTNH 2.9.0)

| Step | Time |
|---|---|
| `prepare` on an unpacked dir, warm cache (67 libraries + client jar + index 60.3 MB, 674 asset objects 112.2 MB) | 0.1 s |
| Boots to main menu | 31 s and 44 s (`The game loaded in 31.257 seconds`, `43.669 seconds`) |
| `export` with the `gtnh-zoo` dump | 53 s (build 2 s, prepare 0 s, client run 51 s): 4,087 blocks, 61,468 states from 138 mods |

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
- `lock`: the client lock.
