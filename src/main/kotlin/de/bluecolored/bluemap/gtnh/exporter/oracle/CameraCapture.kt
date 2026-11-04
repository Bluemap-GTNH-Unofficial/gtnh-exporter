package de.bluecolored.bluemap.gtnh.exporter.oracle

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonPrimitive
import cpw.mods.fml.common.FMLCommonHandler
import cpw.mods.fml.common.Loader
import cpw.mods.fml.common.eventhandler.EventPriority
import cpw.mods.fml.common.eventhandler.SubscribeEvent
import cpw.mods.fml.common.gameevent.TickEvent
import cpw.mods.fml.relauncher.ReflectionHelper
import de.bluecolored.bluemap.gtnh.exporter.ExporterMod
import de.bluecolored.bluemap.gtnh.exporter.PackWriter
import net.minecraft.client.Minecraft
import net.minecraft.client.gui.GuiDisconnected
import net.minecraft.client.gui.GuiDownloadTerrain
import net.minecraft.client.gui.GuiMainMenu
import net.minecraft.client.gui.GuiScreen
import net.minecraft.client.renderer.ActiveRenderInfo
import net.minecraft.client.renderer.EntityRenderer
import net.minecraft.client.renderer.OpenGlHelper
import net.minecraft.client.renderer.RenderGlobal
import net.minecraft.client.renderer.entity.RenderManager
import net.minecraft.client.renderer.texture.DynamicTexture
import net.minecraft.client.renderer.texture.TextureAtlasSprite
import net.minecraft.client.renderer.texture.TextureMap
import net.minecraft.client.renderer.texture.TextureUtil
import net.minecraft.client.resources.data.AnimationMetadataSection
import net.minecraft.command.ICommandSender
import net.minecraft.entity.EntityTracker
import net.minecraft.server.MinecraftServer
import net.minecraft.server.management.PlayerManager
import net.minecraft.server.management.ServerConfigurationManager
import net.minecraft.util.ChatComponentText
import net.minecraft.util.ChunkCoordinates
import net.minecraft.util.IChatComponent
import net.minecraft.util.Timer
import net.minecraft.world.World
import net.minecraft.world.WorldServer
import net.minecraft.world.chunk.EmptyChunk
import net.minecraftforge.client.event.EntityViewRenderEvent
import net.minecraftforge.client.event.FOVUpdateEvent
import net.minecraftforge.client.event.RenderHandEvent
import net.minecraftforge.client.event.RenderWorldLastEvent
import net.minecraftforge.common.MinecraftForge
import net.minecraftforge.event.world.WorldEvent
import org.lwjgl.BufferUtils
import org.lwjgl.opengl.GL11
import org.lwjgl.opengl.GL30
import java.awt.image.BufferedImage
import java.io.File
import java.lang.management.ManagementFactory
import java.lang.reflect.Field
import java.lang.reflect.Method
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.Collections
import java.util.IdentityHashMap
import java.util.Locale
import java.util.concurrent.ConcurrentLinkedQueue
import javax.imageio.ImageIO
import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min

/**
 * `-Dbluemap.gtnh.exporter.mode=cameras`: reference screenshots of scene worlds for the world-scene oracle
 * (`e2e oracle world`); `gtnh-client cameras --jobs` sets everything up.
 *
 * The spec lists jobs, each a scene world the launcher copied into `saves/`. From the main menu the client opens each
 * job's save as a single-player world (integrated server, no dedicated server or RCON), captures its cameras, returns
 * to the main menu, waits until the integrated server's thread has ended and opens the next save, all in one client
 * session. A job whose integrated server does not start or stops fails at once and the next job starts. Before the
 * player joins, the integrated server runs `bmh random-ticks off` (the harness mod `bmh` is loaded into the client for
 * this; it also keeps the scene's frozen tile entities unticked), sets the job's `remove` cells to air with `bmh set`,
 * noon, clear weather, no daylight cycle and no mob spawning. Its chunk radius is the spec's `viewDistance` (the
 * dedicated scene server's), not the client's render distance.
 *
 * Per camera: `tp` on the integrated server, pin position and rotation on the client every tick and frame, wait until
 * every chunk of the scene box within the view distance has been on the client for `loadTicks` ticks, then freeze the
 * game (single-player pause behind an invisible screen: no world, entity, tile entity, particle or texture ticks, the
 * integrated server stops ticking; partial ticks 0, client total world time 0), wait until the chunk renderer reports
 * no pending work and `stableFrames` frames at least `stableIntervalSeconds` apart agree, and capture that frame in
 * [RenderWorldLastEvent] (before other mods draw overlays, with the hand suppressed so the depth buffer stays intact):
 * colour (`<view>.png`), depth (`<view>.depth.bin`, float32 little endian, rows top to bottom, GL window depth 0..1)
 * and the projection and modelview matrices plus the camera position (`<view>.json`). Each job's
 * `cameras-report.json` summarises it; after the last job the client quits (exit 1 if any camera or job failed).
 * A crash report written while a job runs fails the job ([checkCrash]): GTNH's BetterCrashes keeps a crashed client
 * running, so its frames would otherwise be captured from a broken renderer or never come.
 *
 * Capture conditions (see the launcher README): no GUI, hand or block outline; flat lighting (smooth lighting off) with
 * a white lightmap (full brightness everywhere, vanilla's per-face shading 1.0 / 0.8 / 0.6 / 0.5 stays); no fog
 * (pushed beyond the far plane), no clouds, no view bobbing, no particles (cleared every frame); noon and clear
 * weather; FOV exactly the camera's vertical FOV (the flying FOV boost is cancelled); animated block textures pinned to
 * their first frame, as the e2e captures of the BlueMap webapp do.
 *
 * System properties: `bluemap.gtnh.exporter.cameras` (spec JSON written by `gtnh-client cameras`).
 */
object CameraCapture {
    private val log = ExporterMod.log

    private const val EYE_HEIGHT = 1.62 // EntityPlayer.yOffset: the client's posY is the eye, `tp` takes the feet
    private const val MENU_TICKS = 20 // at the main menu this long before the first world is opened
    private const val JOIN_SETTLE_TICKS = 10
    private const val TELEPORT_TIMEOUT_SECONDS = 30.0
    private const val SERVER_STOP_TIMEOUT_SECONDS = 60.0 // the previous integrated server's thread must end by then
    private const val WAIT_LOG_SECONDS = 30.0
    private const val STALL_LOG_SECONDS = 30.0 // the watchdog logs the thread stacks this often while the capture is stuck
    // Minecraft's Timer drops the time of a frame longer than 1 s (Timer.updateTimer): with such frames no client tick
    // runs at all, so the capture then steps on frames once this long has passed without a step
    private const val STEP_STARVED_SECONDS = 1.0
    private const val CRASH_CHECK_SECONDS = 1.0 // how often the capture looks for a new crash report
    private const val CRASH_REPORT_LINES = 200 // the report's head with its description, exception and block details
    private const val CHANGED_CHANNEL_DELTA = 8
    private const val FOG_DISTANCE = 1.0e6f
    private const val FROZEN_WORLD_TIME = 0L // client total world time while frozen: animated TESRs at "frame 0"

    private class Camera(
        val name: String,
        val x: Double,
        val y: Double,
        val z: Double,
        val yaw: Float,
        val pitch: Float,
        val fov: Float,
        val width: Int,
        val height: Int,
    )

    private class Job(
        val scene: String,
        val save: String,
        val box: IntArray?,
        val cameras: List<Camera>,
        val remove: List<IntArray>,
        val freezeRandomTicks: Boolean,
        val outDir: File,
    )

    private class Spec(
        val jobs: List<Job>,
        val captureVersion: Int,
        val viewDistance: Int,
        val renderDistance: Int,
        val loadTicks: Int,
        val stableFrames: Int,
        val stableIntervalSeconds: Double,
        val stableTolerance: Double,
        val worldTimeoutSeconds: Double,
        val cameraTimeoutSeconds: Double,
        val stableTimeoutSeconds: Double,
        val session: File?,
    )

    private enum class Phase { MENU, JOINING, SETUP, TELEPORT, LOAD, STABILIZE, CAPTURE, LEAVE, FINISHED }

    private lateinit var spec: Spec
    @Volatile private var phase = Phase.MENU
    private var phaseTicks = 0
    private var phaseStarted = 0L
    private var waitLogs = 0 // logWaiting lines of the current phase
    private var started = 0L
    private var menuTicks = 0
    private var bootSeconds = Double.NaN
    @Volatile private var lastStep = 0L // read by the watchdog thread

    // per job
    private var jobIndex = -1
    private var jobStarted = 0L
    private var jobTimings = JsonObject()
    private var jobError: String? = null
    private var joinedTicks = 0
    private var index = 0
    @Volatile private var playerName = ""
    private var results = JsonArray()
    private var failures = 0
    private val sessionJobs = JsonArray()
    private var failedJobs = 0
    private var crashReportsBefore: Set<String> = emptySet() // crash-reports/ when the job started
    private var crashCheckedAt = 0L

    @Volatile private var setupDone = false
    @Volatile private var serverError: String? = null
    @Volatile private var teleportReply = ""
    private val serverTasks = ConcurrentLinkedQueue<(MinecraftServer) -> Unit>()

    /** The server's side of the player's chunks: square centre, chunks already sent, chunks still queued. */
    private class ServerView(val centreX: Int, val centreZ: Int, val sent: LongArray, val queued: Int)

    /** Published by the integrated server thread every tick while a camera waits for chunks ([viewWanted]). */
    @Volatile private var serverView: ServerView? = null
    @Volatile private var viewWanted = false

    // per camera
    private var cameraStarted = 0L
    private var pinned: Camera? = null
    private var loadedTicks = 0
    private var loadSeconds = Double.NaN
    private var lastChunks = intArrayOf(0, 0, 0, 0)
    private var lastTerrain: Boolean? = null
    private var frozen = false
    private var frozenFrames = 0
    private var lastSnapshot: ByteArray? = null
    private var lastSnapshotAt = 0L
    private var stableCount = 0
    private var changes = mutableListOf<Double>()
    private var frames = 0
    private var captureRequested = false
    private var stableTimedOut = false

    private val frozenSprites: MutableSet<TextureAtlasSprite> = Collections.newSetFromMap(IdentityHashMap())

    /** Pauses a single-player game (`doesGuiPauseGame`) and draws nothing. */
    private object FreezeScreen : GuiScreen() {
        override fun doesGuiPauseGame() = true
        override fun drawScreen(mouseX: Int, mouseY: Int, partialTicks: Float) {}
        override fun keyTyped(typedChar: Char, keyCode: Int) {}
    }

    /** Runs server commands with full permissions and keeps their replies. */
    private class Sender(private val server: MinecraftServer) : ICommandSender {
        val replies = mutableListOf<String>()
        override fun getCommandSenderName() = "BlueMapOracle"
        override fun func_145748_c_(): IChatComponent = ChatComponentText(commandSenderName)
        override fun addChatMessage(message: IChatComponent) {
            replies += message.unformattedText
        }
        override fun canCommandSenderUseCommand(permissionLevel: Int, command: String) = true
        override fun getPlayerCoordinates() = ChunkCoordinates(0, 0, 0)
        override fun getEntityWorld(): World = server.entityWorld
    }

    private val lightmapTexture: Field = ReflectionHelper.findField(EntityRenderer::class.java, "lightmapTexture", "field_78513_d")
    private val lightmapUpdateNeeded: Field =
        ReflectionHelper.findField(EntityRenderer::class.java, "lightmapUpdateNeeded", "field_78536_aa")
    private val fovModifierHand: Field = ReflectionHelper.findField(EntityRenderer::class.java, "fovModifierHand", "field_78507_R")
    private val fovModifierHandPrev: Field =
        ReflectionHelper.findField(EntityRenderer::class.java, "fovModifierHandPrev", "field_78506_S")
    private val listAnimatedSprites: Field = ReflectionHelper.findField(TextureMap::class.java, "listAnimatedSprites", "field_94258_i")
    private val frameCounter: Field = ReflectionHelper.findField(TextureAtlasSprite::class.java, "frameCounter", "field_110973_g")
    private val tickCounter: Field = ReflectionHelper.findField(TextureAtlasSprite::class.java, "tickCounter", "field_110983_h")
    private val animationMetadata: Field =
        ReflectionHelper.findField(TextureAtlasSprite::class.java, "animationMetadata", "field_110982_k")
    private val ariModelview: Field = ReflectionHelper.findField(ActiveRenderInfo::class.java, "modelview", "field_74594_j")
    private val ariProjection: Field = ReflectionHelper.findField(ActiveRenderInfo::class.java, "projection", "field_74595_k")
    private val worldRenderersToUpdate: Field =
        ReflectionHelper.findField(RenderGlobal::class.java, "worldRenderersToUpdate", "field_72767_j")
    private val timer: Field = ReflectionHelper.findField(Minecraft::class.java, "timer", "field_71428_T")
    private val playersViewDistance: Field =
        ReflectionHelper.findField(ServerConfigurationManager::class.java, "viewDistance", "field_72402_d")
    private val entityViewDistance: Field = ReflectionHelper.findField(EntityTracker::class.java, "entityViewDistance", "field_72792_d")

    /** Angelica's Celeritas chunk renderer (GTNH's default), looked up by name: no compile-time dependency. */
    private val celeritas: Pair<Method, Method>? by lazy {
        try {
            val renderer = Class.forName("com.gtnewhorizons.angelica.rendering.celeritas.CeleritasWorldRenderer")
            renderer.getMethod("getInstanceOrNull") to renderer.getMethod("isTerrainRenderComplete")
        } catch (_: Throwable) {
            null
        }
    }
    private var terrainSource = "none"

    fun start() {
        val specPath = System.getProperty("bluemap.gtnh.exporter.cameras")
            ?: throw IllegalStateException("camera mode needs -Dbluemap.gtnh.exporter.cameras=<spec.json>")
        spec = readSpec(File(specPath))
        val mc = Minecraft.getMinecraft()
        applySettings(mc)
        started = System.nanoTime()
        phaseStarted = started
        FMLCommonHandler.instance().bus().register(this)
        MinecraftForge.EVENT_BUS.register(this)
        startWatchdog(Thread.currentThread())
        log.info("Camera mode: {} jobs, {} cameras", spec.jobs.size, spec.jobs.sumOf { it.cameras.size })
    }

    private fun readSpec(file: File): Spec {
        val json = JsonParser().parse(file.readText(Charsets.UTF_8)).asJsonObject
        val jobs = json.getAsJsonArray("jobs").map { element ->
            val j = element.asJsonObject
            val cameras = j.getAsJsonArray("cameras").map { ce ->
                val c = ce.asJsonObject
                val eye = c.getAsJsonArray("eye")
                Camera(
                    name = c.get("name").asString,
                    x = eye[0].asDouble,
                    y = eye[1].asDouble,
                    z = eye[2].asDouble,
                    yaw = c.get("yaw").asFloat,
                    pitch = c.get("pitch").asFloat,
                    fov = c.get("fov").asFloat,
                    width = c.get("width").asInt,
                    height = c.get("height").asInt,
                )
            }
            require(cameras.isNotEmpty()) { "job ${j.get("scene")} has no cameras" }
            Job(
                scene = j.get("scene").asString,
                save = j.get("save").asString,
                box = j.getAsJsonArray("box")?.map { it.asInt }?.toIntArray(),
                cameras = cameras,
                remove = j.getAsJsonArray("remove")?.map { p -> p.asJsonArray.map { it.asInt }.toIntArray() } ?: emptyList(),
                freezeRandomTicks = j.get("freezeRandomTicks")?.asBoolean ?: true,
                outDir = File(j.get("out").asString).absoluteFile,
            )
        }
        require(jobs.isNotEmpty()) { "no jobs in $file" }
        fun int(key: String, default: Int) = json.get(key)?.asInt ?: default
        fun double(key: String, default: Double) = json.get(key)?.asDouble ?: default
        return Spec(
            jobs = jobs,
            captureVersion = int("captureVersion", 0),
            viewDistance = int("viewDistance", 8),
            renderDistance = int("renderDistance", 12),
            loadTicks = int("loadTicks", 5),
            stableFrames = int("stableFrames", 3),
            stableIntervalSeconds = double("stableIntervalSeconds", 0.1),
            stableTolerance = double("stableTolerance", 0.0005),
            worldTimeoutSeconds = double("worldTimeoutSeconds", 300.0),
            cameraTimeoutSeconds = double("cameraTimeoutSeconds", 60.0),
            stableTimeoutSeconds = double("stableTimeoutSeconds", 30.0),
            session = json.get("session")?.asString?.let(::File),
        )
    }

    /** In memory only (options.txt is not saved); the launcher writes the same values for the run and restores the file. */
    private fun applySettings(mc: Minecraft) {
        val gs = mc.gameSettings
        gs.renderDistanceChunks = spec.renderDistance
        gs.ambientOcclusion = 0
        gs.fancyGraphics = true
        gs.clouds = false
        gs.viewBobbing = false
        gs.particleSetting = 2
        gs.pauseOnLostFocus = false
        gs.showInventoryAchievementHint = false
        gs.hideGUI = true
        gs.fboEnable = true
        gs.anaglyph = false
        gs.thirdPersonView = 0
        gs.mipmapLevels = 0
        gs.anisotropicFiltering = 1
        // the block atlas is stitched after mod init: no mipmaps, so texels stay as sharp as BlueMap's nearest sampling
        mc.textureMapBlocks?.setMipmapLevels(0)
        mc.textureMapBlocks?.setAnisotropicFiltering(1)
    }

    private fun settingsJson(mc: Minecraft) = JsonObject().apply {
        val gs = mc.gameSettings
        addProperty("renderDistanceChunks", gs.renderDistanceChunks)
        addProperty("viewDistance", spec.viewDistance)
        addProperty("ambientOcclusion", gs.ambientOcclusion)
        addProperty("fancyGraphics", gs.fancyGraphics)
        addProperty("clouds", gs.clouds)
        addProperty("viewBobbing", gs.viewBobbing)
        addProperty("particleSetting", gs.particleSetting)
        addProperty("mipmapLevels", gs.mipmapLevels)
        addProperty("anisotropicFiltering", gs.anisotropicFiltering)
        addProperty("framebuffer", OpenGlHelper.isFramebufferEnabled())
        addProperty("lightmap", "white (full brightness)")
        addProperty("fog", "pushed to $FOG_DISTANCE blocks")
        addProperty("animations", "block atlas sprites pinned to frame 0")
        addProperty("particles", "cleared every frame")
        addProperty("freeze", "single-player pause, partial ticks 0, client total world time $FROZEN_WORLD_TIME")
        addProperty("world", "single-player (integrated server)")
        addProperty("terrainRenderer", terrainSource)
        Loader.instance().indexedModList["angelica"]?.let { addProperty("angelica", it.version) }
        Loader.instance().indexedModList["bmh"]?.let { addProperty("bmh", it.version) }
    }

    private fun enter(next: Phase) {
        phase = next
        phaseTicks = 0
        phaseStarted = System.nanoTime()
        waitLogs = 0
    }

    private fun seconds(since: Long) = (System.nanoTime() - since) / 1e9

    private val job: Job get() = spec.jobs[jobIndex]
    private val camera: Camera get() = job.cameras[index]
    private val inWorld: Boolean get() = phase > Phase.JOINING && phase < Phase.LEAVE

    private fun fmt(v: Double) = String.format(Locale.ROOT, "%.4f", v) // a '.' keeps `tp` from adding 0.5

    // ---- server side (integrated server thread) ----

    /** Runs on the integrated server thread at the start of its next tick. */
    private fun onServer(task: (MinecraftServer) -> Unit) {
        serverTasks += task
    }

    private fun command(server: MinecraftServer, command: String): String {
        val sender = Sender(server)
        server.commandManager.executeCommand(sender, command)
        val reply = sender.replies.joinToString(" | ")
        log.info("Server command {} -> {}", command, reply)
        return reply
    }

    private fun bmh(server: MinecraftServer, command: String) {
        val reply = command(server, "bmh $command")
        check(reply.startsWith("OK")) { "bmh $command: $reply" }
    }

    @SubscribeEvent
    fun onServerTick(event: TickEvent.ServerTickEvent) {
        if (event.phase != TickEvent.Phase.START) return
        val server = MinecraftServer.getServer() ?: return
        while (true) {
            val task = serverTasks.poll() ?: break
            try {
                task(server)
            } catch (t: Throwable) {
                log.error("Camera capture server task failed", t)
                serverError = "${t.javaClass.simpleName}: ${t.message}"
            }
        }
        if (viewWanted) publishServerView(server)
    }

    /**
     * Publishes [serverView]: the chunks the server has sent the player (watched and no longer queued) and how many it
     * still has queued. `PlayerManager` moves a player's square only after a move of 8 blocks from its `managedPos`, so
     * the client waits for exactly what the server sends rather than for a square it computes itself.
     */
    private fun publishServerView(server: MinecraftServer) {
        val player = server.configurationManager?.func_152612_a(playerName) ?: return
        val world = player.worldObj as WorldServer
        val cx = player.managedPosX.toInt() shr 4
        val cz = player.managedPosZ.toInt() shr 4
        val range = spec.viewDistance + 1
        val sent = ArrayList<Long>()
        for (x in cx - range..cx + range) for (z in cz - range..cz + range) {
            if (world.playerManager.isPlayerWatchingChunk(player, x, z)) sent += chunkKey(x, z)
        }
        serverView = ServerView(cx, cz, sent.toLongArray(), player.loadedChunks.size)
    }

    private fun chunkKey(x: Int, z: Int) = (x.toLong() shl 32) or (z.toLong() and 0xffffffffL)

    /**
     * Loads the chunk square around a teleport destination before the player gets there. Besides having the chunks
     * ready to send, this takes them off the unload queue: every pause of the single-player game saves the worlds, and
     * `WorldServer.saveAllChunks` queues every loaded chunk no player watches for unloading, among them chunks around
     * the next camera (the server ticks a larger area than it sends). Unloading the chunk the player was just moved
     * into unloads the player entity with it, which then never ticks again and never receives another chunk.
     */
    private fun preloadSquare(server: MinecraftServer, x: Double, z: Double) {
        val player = server.configurationManager?.func_152612_a(playerName) ?: return
        val provider = (player.worldObj as WorldServer).theChunkProviderServer
        val cx = x.toInt() shr 4
        val cz = z.toInt() shr 4
        val range = spec.viewDistance
        for (i in cx - range..cx + range) for (j in cz - range..cz + range) provider.loadChunk(i, j)
    }

    /**
     * The integrated server sends the chunks within [Spec.viewDistance] like the dedicated scene server did: its player
     * list keeps the client's render distance (so `IntegratedServer.tick` does not reset it), while every world's chunk
     * radius and entity tracking range follow the view distance.
     */
    @SubscribeEvent
    fun onWorldLoad(event: WorldEvent.Load) {
        val world = event.world as? WorldServer ?: return
        if (jobIndex < 0 || phase == Phase.FINISHED) return
        val players = world.func_73046_m().configurationManager ?: return
        playersViewDistance.setInt(players, spec.renderDistance)
        world.playerManager.func_152622_a(spec.viewDistance)
        entityViewDistance.setInt(world.entityTracker, PlayerManager.getFurthestViewableBlock(spec.viewDistance))
    }

    // ---- client side ----

    @SubscribeEvent
    fun onClientTick(event: TickEvent.ClientTickEvent) {
        if (phase == Phase.FINISHED) return
        val mc = Minecraft.getMinecraft()
        if (event.phase == TickEvent.Phase.START) {
            try {
                pin(mc)
            } catch (t: Throwable) {
                failed(mc, t)
            }
            return
        }
        advance(mc)
    }

    /** One step of the capture: every client tick, and every frame while no client tick runs ([onRenderTick]). */
    private fun advance(mc: Minecraft) {
        lastStep = System.nanoTime()
        try {
            pin(mc)
            phaseTicks++
            step(mc)
            if (inWorld) ensureSize(mc, rebind = false)
        } catch (t: Throwable) {
            failed(mc, t)
        }
    }

    private fun failed(mc: Minecraft, t: Throwable) {
        log.fatal("Camera capture failed", t)
        if (jobIndex >= 0 && inWorld || phase == Phase.JOINING) jobFailed(mc, "${t.javaClass.simpleName}: ${t.message}")
        else finishSession(mc, "${t.javaClass.simpleName}: ${t.message}")
    }

    private fun step(mc: Minecraft) {
        if (inWorld || phase == Phase.JOINING) {
            checkCrash(mc)
            serverError?.let { throw IllegalStateException("integrated server: $it") }
            val server = mc.integratedServer
            if (server == null || server.isServerStopped) {
                // a server that failed to start or crashed: its errors are in the log above (and in crash-reports/)
                throw IllegalStateException(
                    if (phase == Phase.JOINING) "the integrated server did not start or stopped before the player joined"
                    else "the integrated server stopped (screen ${mc.currentScreen?.javaClass?.name})",
                )
            }
            if (phase != Phase.JOINING && (mc.theWorld == null || mc.currentScreen is GuiDisconnected)) {
                throw IllegalStateException("left the world (screen ${mc.currentScreen?.javaClass?.name})")
            }
            if (phase == Phase.JOINING) logWaiting("job ${job.scene}: joining the world") {
                "screen ${mc.currentScreen?.javaClass?.name}, client world ${mc.theWorld != null}, setup done $setupDone"
            }
        }
        when (phase) {
            Phase.MENU -> {
                if (mc.currentScreen != null && mc.theWorld == null && ++menuTicks >= MENU_TICKS) {
                    bootSeconds = (System.currentTimeMillis() - ManagementFactory.getRuntimeMXBean().startTime) / 1000.0
                    check(Loader.isModLoaded("bmh")) { "the harness mod bmh is not loaded (gtnh-client cameras installs it)" }
                    startJob(mc, 0)
                }
            }
            Phase.JOINING -> {
                val screen = mc.currentScreen
                if (screen is GuiDisconnected) throw IllegalStateException("disconnected from the integrated server")
                if (mc.theWorld != null && mc.thePlayer != null && screen !is GuiDownloadTerrain && setupDone) {
                    if (++joinedTicks >= JOIN_SETTLE_TICKS) {
                        playerName = mc.thePlayer.commandSenderName
                        jobTimings.addProperty("join", seconds(phaseStarted))
                        val name = playerName
                        onServer { command(it, "gamemode 1 $name") }
                        enter(Phase.SETUP)
                    }
                } else {
                    joinedTicks = 0
                }
                if (seconds(phaseStarted) > spec.worldTimeoutSeconds) {
                    throw IllegalStateException("not in the world after ${spec.worldTimeoutSeconds} s (screen ${screen?.javaClass?.name})")
                }
            }
            Phase.SETUP -> {
                val player = mc.thePlayer
                if (player.capabilities.allowFlying) {
                    player.capabilities.isFlying = true
                    player.sendPlayerAbilities()
                    startCamera(mc)
                } else if (seconds(phaseStarted) > TELEPORT_TIMEOUT_SECONDS) {
                    throw IllegalStateException("creative mode did not reach the client")
                }
            }
            Phase.TELEPORT -> {
                val c = camera
                val p = mc.thePlayer
                if (abs(p.posX - c.x) < 1.0 && abs(p.posY - c.y) < 1.0 && abs(p.posZ - c.z) < 1.0) {
                    pinned = c
                    enter(Phase.LOAD)
                } else if (seconds(phaseStarted) > TELEPORT_TIMEOUT_SECONDS) {
                    cameraFailed(mc, "teleport not confirmed by the server (reply: ${teleportReply.trim()})")
                }
            }
            Phase.LOAD -> {
                lastChunks = chunkStatus(mc)
                loadedTicks = if (chunksReady) loadedTicks + 1 else 0
                if (loadedTicks >= spec.loadTicks) {
                    loadSeconds = seconds(cameraStarted)
                    freeze(mc)
                    enter(Phase.STABILIZE)
                } else if (seconds(cameraStarted) > spec.cameraTimeoutSeconds) {
                    // a capture without its terrain is no reference: fail the camera (and so the job) instead
                    val view = if (lastChunks[3] < 0) "the server's chunk square never moved to the camera"
                    else "${lastChunks[3]} of the view square's chunks"
                    cameraFailed(mc, "chunks still missing after ${spec.cameraTimeoutSeconds} s: ${lastChunks[1]} of the " +
                        "${lastChunks[0]} scene box chunks in range, $view")
                }
            }
            Phase.STABILIZE -> {
                // frames are compared in onRenderWorldLast; this only bounds the wait
                if (!captureRequested && seconds(phaseStarted) > spec.stableTimeoutSeconds) {
                    log.warn("Camera {}: frames still changing after {} s (terrain complete {}, {})", camera.name,
                        spec.stableTimeoutSeconds, lastTerrain, changes.takeLast(8))
                    stableTimedOut = true
                    captureRequested = true
                }
                if (captureRequested) enter(Phase.CAPTURE)
            }
            Phase.CAPTURE -> {
                // the capture itself happens in the next RenderWorldLastEvent of the right size
                if (!captureRequested) nextCamera(mc)
                else if (seconds(phaseStarted) > TELEPORT_TIMEOUT_SECONDS) cameraFailed(mc, "no frame of the camera size was rendered")
            }
            Phase.LEAVE -> leaveWorld(mc)
            Phase.FINISHED -> {}
        }
    }

    private fun startJob(mc: Minecraft, next: Int) {
        jobIndex = next
        val j = job
        jobStarted = System.nanoTime()
        jobTimings = JsonObject()
        jobError = null
        joinedTicks = 0
        index = 0
        results = JsonArray()
        failures = 0
        setupDone = false
        serverError = null
        serverTasks.clear()
        crashReportsBefore = crashReports(mc)
        crashCheckedAt = System.nanoTime()
        j.outDir.mkdirs()
        log.info("Camera job {}/{}: scene {} (save {}), {} cameras -> {}", next + 1, spec.jobs.size, j.scene, j.save,
            j.cameras.size, j.outDir)
        val levelDat = File(mc.mcDataDir, "saves/${j.save}/level.dat")
        if (!levelDat.isFile) {
            jobError = "$levelDat does not exist"
            writeJobReport(mc)
            enter(Phase.LEAVE)
            return
        }
        // runs at the integrated server's first tick, before the player has joined
        onServer { server ->
            bmh(server, "random-ticks " + if (j.freezeRandomTicks) "off" else "on")
            for (cell in j.remove) bmh(server, "set 0 ${cell[0]} ${cell[1]} ${cell[2]} minecraft:air 0")
            command(server, "time set 6000")
            command(server, "weather clear 1000000")
            command(server, "gamerule doDaylightCycle false")
            command(server, "gamerule doMobSpawning false")
            setupDone = true
        }
        enter(Phase.JOINING)
        val opening = System.nanoTime()
        mc.launchIntegratedServer(j.save, j.scene, null) // blocks until the integrated server runs
        jobTimings.addProperty("open", seconds(opening))
        phaseStarted = System.nanoTime()
    }

    private fun startCamera(mc: Minecraft) {
        val c = camera
        pinned = null
        mc.gameSettings.fovSetting = c.fov
        cameraStarted = System.nanoTime()
        loadedTicks = 0
        loadSeconds = Double.NaN
        lastTerrain = null
        frozen = false
        frozenFrames = 0
        lastSnapshot = null
        lastSnapshotAt = 0L
        stableCount = 0
        changes = mutableListOf()
        frames = 0
        captureRequested = false
        stableTimedOut = false
        teleportReply = ""
        serverView = null
        viewWanted = true
        val name = playerName
        onServer {
            preloadSquare(it, c.x, c.z)
            teleportReply = command(it, "tp $name ${fmt(c.x)} ${fmt(c.y - EYE_HEIGHT)} ${fmt(c.z)}")
        }
        log.info("Camera {} ({}/{}): eye {} {} {}, yaw {}, pitch {}, fov {}", c.name, index + 1, job.cameras.size, c.x, c.y, c.z,
            c.yaw, c.pitch, c.fov)
        enter(Phase.TELEPORT)
    }

    /** Pauses the game behind [FreezeScreen]: the world, its entities, tile entities, particles and textures stop. */
    private fun freeze(mc: Minecraft) {
        frozen = true
        viewWanted = false
        frozenFrames = 0
        mc.displayGuiScreen(FreezeScreen)
    }

    private fun unfreeze(mc: Minecraft) {
        if (!frozen) return
        frozen = false
        if (mc.currentScreen === FreezeScreen) mc.displayGuiScreen(null)
    }

    private fun nextCamera(mc: Minecraft) {
        unfreeze(mc)
        index++
        if (index < job.cameras.size) startCamera(mc) else finishJob(mc)
    }

    private fun cameraFailed(mc: Minecraft, reason: String) {
        log.error("Camera {} failed: {}", camera.name, reason)
        failures++
        results.add(JsonObject().apply {
            addProperty("name", camera.name)
            addProperty("captured", false)
            addProperty("error", reason)
            addProperty("seconds", seconds(cameraStarted))
        })
        captureRequested = false
        nextCamera(mc)
    }

    /** The job cannot go on (server error, disconnect, timeout): its remaining cameras fail, the next job starts. */
    private fun jobFailed(mc: Minecraft, reason: String) {
        if (phase == Phase.LEAVE || phase == Phase.FINISHED || jobIndex < 0) return
        log.error("Camera job {} failed: {}", job.scene, reason)
        jobError = reason
        captureRequested = false
        pinned = null
        unfreeze(mc)
        finishJob(mc)
    }

    private fun finishJob(mc: Minecraft) {
        pinned = null
        jobTimings.addProperty("cameras", results.sumOf { it.asJsonObject.get("seconds").asDouble })
        writeJobReport(mc)
        enter(Phase.LEAVE)
    }

    private fun writeJobReport(mc: Minecraft) {
        val j = job
        val captured = results.count { it.asJsonObject.get("captured").asBoolean }
        if (jobError != null || captured < j.cameras.size) failedJobs++
        val report = JsonObject().apply {
            addProperty("scene", j.scene)
            addProperty("capture_version", spec.captureVersion)
            addProperty("cameras", j.cameras.size)
            addProperty("captured", captured)
            addProperty("failed", j.cameras.size - captured)
            jobError?.let { addProperty("error", it) }
            addProperty("seconds", seconds(jobStarted))
            add("timings", jobTimings)
            add("settings", settingsJson(mc))
            add("results", results)
        }
        j.outDir.mkdirs()
        File(j.outDir, "cameras-report.json").writeText(PackWriter.PRETTY_GSON.toJson(report))
        sessionJobs.add(JsonObject().apply {
            addProperty("scene", j.scene)
            addProperty("out", j.outDir.path)
            addProperty("captured", captured)
            addProperty("failed", j.cameras.size - captured)
            jobError?.let { addProperty("error", it) }
            addProperty("seconds", seconds(jobStarted))
            add("timings", jobTimings)
        })
    }

    /**
     * Back to the main menu (the integrated server saves and stops), then the next job or the end of the session. The
     * next save is opened only once the previous integrated server's thread has ended: `loadWorld(null)` returns when
     * the server reports itself stopped, but its thread still runs the mods' `FMLServerStoppedEvent` handlers. A server
     * started meanwhile loses `MinecraftServer.getServer()` (MineTweaker's `FMLServerAboutToStartEvent` then throws),
     * FML's state machine errs and the save never opens.
     */
    private fun leaveWorld(mc: Minecraft) {
        if (phaseTicks == 1) {
            unfreeze(mc)
            mc.theWorld?.sendQuittingDisconnectingPacket()
            // also a server whose player never joined: it must not run into the next job
            if (mc.theWorld != null || mc.integratedServer != null) mc.loadWorld(null)
            mc.displayGuiScreen(GuiMainMenu())
            return
        }
        val running = serverThreads()
        if (running.isNotEmpty()) {
            if (seconds(phaseStarted) > SERVER_STOP_TIMEOUT_SECONDS) {
                log.error("Camera job {}: the integrated server thread still runs after {} s:\n{}", job.scene,
                    SERVER_STOP_TIMEOUT_SECONDS, running.joinToString("\n") { stackTrace(it) })
                finishSession(mc, "the integrated server of job ${job.scene} did not stop within $SERVER_STOP_TIMEOUT_SECONDS s")
                return
            }
            logWaiting("job ${job.scene}: waiting for the integrated server thread to end") { stackTrace(running[0]) }
            return
        }
        val leave = seconds(phaseStarted)
        (sessionJobs.lastOrNull() as? JsonObject)?.getAsJsonObject("timings")?.addProperty("leave", leave)
        log.info("Camera job {} done in {} s (leaving {} s)", job.scene, seconds(jobStarted), leave)
        if (jobIndex + 1 < spec.jobs.size) startJob(mc, jobIndex + 1) else finishSession(mc, null)
    }

    /** Live integrated server threads (`MinecraftServer.startServerThread` names them "Server thread"). */
    private fun serverThreads(): List<Thread> =
        Thread.getAllStackTraces().keys.filter { it.name == "Server thread" && it.isAlive && it !== Thread.currentThread() }

    private fun stackTrace(thread: Thread) =
        "\"${thread.name}\" ${thread.state}\n" + thread.stackTrace.take(20).joinToString("\n") { "\tat $it" }

    /** A line every [WAIT_LOG_SECONDS] of a phase that is still waiting, so a slow wait shows in the log. */
    private fun logWaiting(what: String, state: () -> String) {
        val waited = seconds(phaseStarted)
        if (waited < (waitLogs + 1) * WAIT_LOG_SECONDS) return
        waitLogs++
        log.info("Camera {} for {} s: {}", what, waited.toInt(), state())
    }

    private fun finishSession(mc: Minecraft, error: String?) {
        if (phase == Phase.FINISHED) return
        phase = Phase.FINISHED
        pinned = null
        writeSession(error)
        if (System.getProperty("bluemap.gtnh.exporter.stay") == "true") return
        if (error == null && failedJobs == 0) {
            log.info("Camera capture finished, shutting down the client")
            mc.shutdown()
        } else {
            log.error("Camera capture finished with errors ({}), exiting with 1", error ?: "$failedJobs jobs failed")
            FMLCommonHandler.instance().exitJava(1, false)
        }
    }

    private fun writeSession(error: String?) {
        val file = spec.session ?: return
        val report = JsonObject().apply {
            addProperty("capture_version", spec.captureVersion)
            addProperty("jobs", spec.jobs.size)
            addProperty("failedJobs", failedJobs + (spec.jobs.size - sessionJobs.size()))
            error?.let { addProperty("error", it) }
            addProperty("bootSeconds", bootSeconds)
            addProperty("seconds", seconds(started))
            add("results", sessionJobs)
        }
        file.parentFile?.mkdirs()
        file.writeText(PackWriter.PRETTY_GSON.toJson(report))
    }

    private fun crashReportDir(mc: Minecraft) = File(mc.mcDataDir, "crash-reports")

    private fun crashReports(mc: Minecraft): Set<String> = crashReportDir(mc).list()?.toSet() ?: emptySet()

    /**
     * Fails the job on a crash report written since it started. GTNH's BetterCrashes catches an exception of the game
     * loop, writes the report, resets the game state and lets the client go on, so the capture would not notice: a
     * block whose rendering throws fails its whole chunk mesh (Angelica's chunk builder), and Angelica's renderer may
     * not survive the reset (`Executor is stopped` every frame, then no frame at all is drawn). The job's error is the
     * report's description and exception plus, for a block that threw while rendered, the block, its meta and location.
     */
    private fun checkCrash(mc: Minecraft) {
        if (seconds(crashCheckedAt) < CRASH_CHECK_SECONDS) return
        crashCheckedAt = System.nanoTime()
        // crash-<yyyy-MM-dd_HH.mm.ss>-<side>.txt: the first in name order is the first crash
        val first = (crashReports(mc) - crashReportsBefore).minOrNull() ?: return
        throw IllegalStateException("the client crashed: " + crashSummary(File(crashReportDir(mc), first)))
    }

    private fun crashSummary(report: File): String {
        val head = runCatching { report.bufferedReader(Charsets.UTF_8).useLines { it.take(CRASH_REPORT_LINES).toList() } }
            .getOrElse { return "${report.name} (unreadable: $it)" }
        val description = head.indexOfFirst { it.startsWith("Description: ") }
        if (description < 0) return "${report.name} (no description in its first ${head.size} lines)"
        val exception = head.drop(description + 1).firstOrNull { it.isNotBlank() }
        fun detail(key: String) = head.firstOrNull { it.startsWith("    $key: ") }?.substringAfter(": ")
        val block = detail("Block")?.let { "; block $it, meta ${detail("Block meta")}, ${detail("Block location")}" } ?: ""
        return "${head[description].removePrefix("Description: ")}: $exception$block (crash-reports/${report.name})"
    }

    /**
     * Every wait of the capture runs on client ticks or frames, so a client thread that is stuck (blocked in, or looping
     * through, a Minecraft or mod call; a single frame that does not end) would wait silently until the launcher's
     * timeout. This thread logs the client and server threads' stacks every [STALL_LOG_SECONDS] while the capture has
     * not stepped, and after [Spec.worldTimeoutSeconds] writes the session report and kills the client ([killClient]).
     */
    private fun startWatchdog(client: Thread) {
        val watchdog = Thread({
            var logged = 0
            while (phase != Phase.FINISHED) {
                Thread.sleep(1000)
                val last = lastStep
                if (last == 0L) continue // still loading: no client tick yet
                val stalled = (System.nanoTime() - last) / 1e9
                if (stalled < STALL_LOG_SECONDS) {
                    logged = 0
                    continue
                }
                if (stalled >= (logged + 1) * STALL_LOG_SECONDS) {
                    logged++
                    log.warn("Camera capture: no step for {} s ({}{}):\n{}", stalled.toInt(), phase,
                        if (jobIndex >= 0) ", job ${job.scene}" else "", threadDump(client))
                }
                if (stalled > spec.worldTimeoutSeconds && phase != Phase.FINISHED) {
                    val error = "the client thread made no progress for ${stalled.toInt()} s ($phase" +
                        (if (jobIndex >= 0) ", job ${job.scene}" else "") + "; its stack is in the log)"
                    log.error("Camera capture failed: {}; killing the client", error)
                    runCatching { writeSession(error) }.onFailure { log.error("Writing the session report failed", it) }
                    System.out.flush()
                    killClient()
                }
            }
        }, "Camera capture watchdog")
        watchdog.isDaemon = true
        watchdog.start()
    }

    /**
     * Ends this JVM without waiting for any of its threads. Neither System.exit (shutdown hooks wait for the stuck
     * threads) nor Runtime.halt (the GL driver's exit handlers wait for the draw call in progress) ends a client stuck
     * in a frame, so the process kills itself from outside: SIGKILL, on Windows `taskkill /F` (TerminateProcess, exit
     * code 1). Should that fail, FML's hard exit: a Runtime.halt or System.exit in mod code throws FML's
     * ExitTrappedException instead (TerminalTransformer), so a mod can only exit through FMLCommonHandler.exitJava.
     */
    private fun killClient() {
        val pid = ProcessHandle.current().pid().toString()
        val kill = if (System.getProperty("os.name").startsWith("Windows")) listOf("taskkill", "/F", "/PID", pid)
        else listOf("kill", "-KILL", pid)
        runCatching { ProcessBuilder(kill).inheritIO().start().waitFor() }
            .onFailure { log.error("Killing the client with `{}` failed", kill.joinToString(" "), it) }
        FMLCommonHandler.instance().exitJava(1, true)
    }

    /** The client thread's stack, the owner of the lock it waits for, and every integrated server thread's. */
    private fun threadDump(client: Thread): String {
        val infos = ManagementFactory.getThreadMXBean().dumpAllThreads(true, true).filterNotNull()
        val clientInfo = infos.firstOrNull { it.threadName == client.name }
        val names = setOfNotNull(client.name, "Server thread", clientInfo?.lockOwnerName)
        return infos.filter { it.threadName in names }.joinToString("\n") { info ->
            "\"${info.threadName}\" ${info.threadState}" + (info.lockName?.let { " on $it" } ?: "") +
                (info.lockOwnerName?.let { " owned by \"$it\"" } ?: "") + "\n" +
                info.stackTrace.take(40).joinToString("\n") { "\tat $it" }
        }
    }

    /** Exact position and rotation, no interpolation between ticks (prev/lastTick = current), no motion. */
    private fun pin(mc: Minecraft) {
        val c = pinned ?: return
        val p = mc.thePlayer ?: return
        p.setPositionAndRotation(c.x, c.y, c.z, c.yaw, c.pitch)
        p.lastTickPosX = c.x
        p.lastTickPosY = c.y
        p.lastTickPosZ = c.z
        p.motionX = 0.0
        p.motionY = 0.0
        p.motionZ = 0.0
        p.rotationYawHead = c.yaw
        p.prevRotationYawHead = c.yaw
        p.renderYawOffset = c.yaw
        p.prevRenderYawOffset = c.yaw
        p.cameraPitch = 0f
        p.prevCameraPitch = 0f
        p.cameraYaw = 0f
        p.prevCameraYaw = 0f
        p.capabilities.isFlying = true
    }

    /** The game framebuffer at the camera's size (the window itself stays minimized and is never resized). */
    private fun ensureSize(mc: Minecraft, rebind: Boolean) {
        val c = pinned ?: job.cameras.getOrNull(index) ?: return
        val fb = mc.framebuffer
        if (mc.displayWidth != c.width || mc.displayHeight != c.height || fb.framebufferWidth != c.width ||
            fb.framebufferHeight != c.height
        ) {
            mc.resize(c.width, c.height)
            // resize() leaves framebuffer 0 bound; during a frame the game framebuffer must be bound again
            if (rebind) fb.bindFramebuffer(true)
        }
    }

    @SubscribeEvent
    fun onRenderTick(event: TickEvent.RenderTickEvent) {
        if (event.phase != TickEvent.Phase.START || !inWorld) return
        val mc = Minecraft.getMinecraft()
        if (mc.theWorld == null || mc.thePlayer == null) return
        if (seconds(lastStep) > STEP_STARVED_SECONDS) {
            advance(mc)
            if (!inWorld) return
        }
        try {
            frames++
            pin(mc)
            ensureSize(mc, rebind = true)
            val screen = if (frozen) FreezeScreen else null
            if (mc.currentScreen !== screen) mc.displayGuiScreen(screen)
            mc.gameSettings.hideGUI = true
            mc.effectRenderer.clearEffects(mc.theWorld)
            // the FOV is the camera's at once (the FOV event already cancels the flying boost the modifier eases to)
            fovModifierHand.setFloat(mc.entityRenderer, 1.0f)
            fovModifierHandPrev.setFloat(mc.entityRenderer, 1.0f)
            if (frozen && mc.isGamePaused) {
                frozenFrames++
                (timer.get(mc) as Timer).renderPartialTicks = 0f
                mc.theWorld.func_82738_a(FROZEN_WORLD_TIME)
            }
            freezeAnimations(mc)
            whiteLightmap(mc)
        } catch (t: Throwable) {
            log.fatal("Camera capture failed", t)
            jobFailed(mc, "${t.javaClass.simpleName}: ${t.message}")
        }
    }

    /** Every animated sprite of the block atlas back to its first frame right before the frame is drawn. */
    private fun freezeAnimations(mc: Minecraft) {
        val atlas = mc.textureMapBlocks ?: return
        @Suppress("UNCHECKED_CAST")
        val sprites = listAnimatedSprites.get(atlas) as List<TextureAtlasSprite>
        var bound = false
        for (sprite in sprites) {
            if (frameCounter.getInt(sprite) == 0 && sprite in frozenSprites) continue
            val first = try {
                (animationMetadata.get(sprite) as AnimationMetadataSection?)?.getFrameIndex(0) ?: 0
            } catch (_: Exception) {
                0
            }
            frameCounter.setInt(sprite, 0)
            tickCounter.setInt(sprite, 0)
            if (first in 0 until sprite.frameCount) {
                if (!bound) {
                    GL11.glBindTexture(GL11.GL_TEXTURE_2D, atlas.glTextureId)
                    bound = true
                }
                TextureUtil.uploadTextureMipmap(
                    sprite.getFrameTextureData(first), sprite.iconWidth, sprite.iconHeight, sprite.originX, sprite.originY, false, false,
                )
            }
            frozenSprites.add(sprite)
        }
    }

    /** Full brightness: every lightmap entry white, and no recomputation for this frame. */
    private fun whiteLightmap(mc: Minecraft) {
        val renderer = mc.entityRenderer
        val texture = lightmapTexture.get(renderer) as DynamicTexture
        val data = texture.textureData
        if (data.any { it != -1 }) {
            data.fill(-1)
            texture.updateDynamicTexture()
        }
        lightmapUpdateNeeded.setBoolean(renderer, false)
    }

    @SubscribeEvent
    fun onRenderHand(event: RenderHandEvent) {
        // no hand, and no depth clear before it: the depth buffer keeps the world for the capture
        if (inWorld) event.isCanceled = true
    }

    @SubscribeEvent
    fun onFog(event: EntityViewRenderEvent.RenderFogEvent) {
        if (!inWorld) return
        GL11.glFogf(GL11.GL_FOG_START, FOG_DISTANCE)
        GL11.glFogf(GL11.GL_FOG_END, FOG_DISTANCE * 2)
    }

    @SubscribeEvent
    fun onFov(event: FOVUpdateEvent) {
        // no FOV boost from flying or speed: the projection uses exactly gameSettings.fovSetting
        if (inWorld) event.newfov = 1.0f
    }

    /** Pixel buffer RGBA of the bound framebuffer, rows bottom to top as GL returns them. */
    private fun readColor(w: Int, h: Int): ByteBuffer {
        val pixels = BufferUtils.createByteBuffer(w * h * 4)
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1)
        GL11.glReadPixels(0, 0, w, h, GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, pixels)
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 4)
        return pixels
    }

    @SubscribeEvent(priority = EventPriority.HIGHEST)
    fun onRenderWorldLast(event: RenderWorldLastEvent) {
        if (phase != Phase.STABILIZE && phase != Phase.CAPTURE) return
        val mc = Minecraft.getMinecraft()
        val c = pinned ?: return
        try {
            // a frame of the frozen game: the pause takes effect one frame after the screen opened
            if (!mc.isGamePaused || frozenFrames < 2) return
            val viewport = BufferUtils.createIntBuffer(16)
            GL11.glGetInteger(GL11.GL_VIEWPORT, viewport)
            if (mc.displayWidth != c.width || mc.displayHeight != c.height || viewport.get(2) != c.width || viewport.get(3) != c.height) return
            if (captureRequested) {
                val pixels = readColor(c.width, c.height)
                capture(mc, c, event.partialTicks, ByteArray(pixels.remaining()).also { pixels.get(it) }, viewport)
                captureRequested = false
                return
            }
            if (phase == Phase.CAPTURE) return // captured: the next step moves on to the next camera
            lastTerrain = terrainComplete(mc)
            if (lastTerrain == false) {
                stableCount = 0
                lastSnapshot = null
                return
            }
            val now = System.nanoTime()
            if (lastSnapshot != null && (now - lastSnapshotAt) / 1e9 < spec.stableIntervalSeconds) return
            val pixels = readColor(c.width, c.height)
            val bytes = ByteArray(pixels.remaining()).also { pixels.get(it) }
            val previous = lastSnapshot
            if (previous != null) {
                val changed = changedRatio(previous, bytes)
                changes.add(changed)
                stableCount = if (changed <= spec.stableTolerance) stableCount + 1 else 0
            }
            lastSnapshot = bytes
            lastSnapshotAt = now
            if (stableCount >= spec.stableFrames - 1) {
                // this frame agrees with the previous ones: it is the capture
                capture(mc, c, event.partialTicks, bytes, viewport)
                captureRequested = false
                enter(Phase.CAPTURE)
            }
        } catch (t: Throwable) {
            log.fatal("Camera capture failed", t)
            jobFailed(mc, "${t.javaClass.simpleName}: ${t.message}")
        }
    }

    private fun changedRatio(a: ByteArray, b: ByteArray): Double {
        var changed = 0
        var i = 0
        while (i < a.size) {
            if (abs((a[i].toInt() and 255) - (b[i].toInt() and 255)) > CHANGED_CHANNEL_DELTA ||
                abs((a[i + 1].toInt() and 255) - (b[i + 1].toInt() and 255)) > CHANGED_CHANNEL_DELTA ||
                abs((a[i + 2].toInt() and 255) - (b[i + 2].toInt() and 255)) > CHANGED_CHANNEL_DELTA
            ) {
                changed++
            }
            i += 4
        }
        return changed.toDouble() / (a.size / 4)
    }

    private fun glMatrix(name: Int): FloatArray {
        val buffer = BufferUtils.createFloatBuffer(16)
        GL11.glGetFloat(name, buffer)
        return FloatArray(16) { buffer.get(it) }
    }

    private fun floats(buffer: FloatBuffer) = FloatArray(16) { buffer.get(it) }

    private fun capture(mc: Minecraft, c: Camera, partialTicks: Float, rgba: ByteArray, viewport: java.nio.IntBuffer) {
        val w = c.width
        val h = c.height
        val outDir = job.outDir
        val depth = BufferUtils.createFloatBuffer(w * h)
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1)
        GL11.glReadPixels(0, 0, w, h, GL11.GL_DEPTH_COMPONENT, GL11.GL_FLOAT, depth)
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 4)
        val modelview = glMatrix(GL11.GL_MODELVIEW_MATRIX)
        val projection = glMatrix(GL11.GL_PROJECTION_MATRIX)
        val framebuffer = GL11.glGetInteger(GL30.GL_FRAMEBUFFER_BINDING)
        val entity = mc.renderViewEntity
        val cameraPos = doubleArrayOf(
            entity.lastTickPosX + (entity.posX - entity.lastTickPosX) * partialTicks,
            entity.lastTickPosY + (entity.posY - entity.lastTickPosY) * partialTicks,
            entity.lastTickPosZ + (entity.posZ - entity.lastTickPosZ) * partialTicks,
        )

        val image = BufferedImage(w, h, BufferedImage.TYPE_INT_RGB)
        val argb = IntArray(w * h)
        val depthOut = ByteBuffer.allocate(w * h * 4).order(ByteOrder.LITTLE_ENDIAN)
        for (y in 0 until h) {
            val row = (h - 1 - y) * w // GL rows are bottom to top
            for (x in 0 until w) {
                val i = (row + x) * 4
                argb[y * w + x] = ((rgba[i].toInt() and 255) shl 16) or ((rgba[i + 1].toInt() and 255) shl 8) or (rgba[i + 2].toInt() and 255)
                depthOut.putFloat(depth.get(row + x))
            }
        }
        image.setRGB(0, 0, w, h, argb, 0, w)
        ImageIO.write(image, "png", File(outDir, "${c.name}.png"))
        File(outDir, "${c.name}.depth.bin").writeBytes(depthOut.array())

        val gs = mc.gameSettings
        val stable = !stableTimedOut && stableCount >= spec.stableFrames - 1
        val cameraSeconds = seconds(cameraStarted)
        val json = JsonObject().apply {
            addProperty("name", c.name)
            addProperty("scene", job.scene)
            addProperty("width", w)
            addProperty("height", h)
            add("eye", JsonArray().also { a -> doubleArrayOf(c.x, c.y, c.z).forEach { a.add(JsonPrimitive(it)) } })
            addProperty("yaw", c.yaw)
            addProperty("pitch", c.pitch)
            addProperty("fov", c.fov)
            add("camera", JsonArray().also { a -> cameraPos.forEach { a.add(JsonPrimitive(it)) } })
            add("renderPos", JsonArray().also { a ->
                doubleArrayOf(RenderManager.renderPosX, RenderManager.renderPosY, RenderManager.renderPosZ)
                    .forEach { a.add(JsonPrimitive(it)) }
            })
            addProperty("partialTicks", partialTicks)
            addProperty("matrixOrder", "column-major (OpenGL); world -> eye = modelview * translate(-camera)")
            add("projection", floatsJson(projection))
            add("modelview", floatsJson(modelview))
            add("activeRenderInfo", JsonObject().apply {
                add("projection", floatsJson(floats(ariProjection.get(null) as FloatBuffer)))
                add("modelview", floatsJson(floats(ariModelview.get(null) as FloatBuffer)))
            })
            addProperty("fovApplied", gs.fovSetting * fovModifierHand.getFloat(mc.entityRenderer))
            addProperty("near", 0.05)
            addProperty("far", gs.renderDistanceChunks * 16.0 * 2.0)
            add("viewport", JsonArray().also { a -> (0 until 4).forEach { a.add(JsonPrimitive(viewport.get(it))) } })
            addProperty("framebufferBinding", framebuffer)
            addProperty("gameFramebuffer", mc.framebuffer.framebufferObject)
            addProperty("image", "${c.name}.png")
            add("depth", JsonObject().apply {
                addProperty("file", "${c.name}.depth.bin")
                addProperty("format", "float32 little endian, rows top to bottom, OpenGL window depth 0..1 (1 = nothing drawn)")
            })
            add("settle", JsonObject().apply {
                addProperty("settled", chunksReady && lastTerrain != false)
                addProperty("seconds", loadSeconds)
                addProperty("chunksRequired", lastChunks[0])
                addProperty("chunksMissing", lastChunks[1])
                addProperty("chunksOutOfRange", lastChunks[2])
                addProperty("viewChunksMissing", lastChunks[3])
                lastTerrain?.let { addProperty("terrainComplete", it) }
                addProperty("terrainRenderer", terrainSource)
            })
            add("stability", JsonObject().apply {
                addProperty("stable", stable)
                addProperty("tolerance", spec.stableTolerance)
                addProperty("intervalSeconds", spec.stableIntervalSeconds)
                add("changedRatios", JsonArray().also { a -> changes.forEach { a.add(JsonPrimitive(it)) } })
            })
            addProperty("frozen", mc.isGamePaused)
            addProperty("frames", frames)
            addProperty("fps", frames / cameraSeconds)
            addProperty("teleport", teleportReply.trim())
            addProperty("seconds", cameraSeconds)
        }
        File(outDir, "${c.name}.json").writeText(PackWriter.PRETTY_GSON.toJson(json))
        results.add(JsonObject().apply {
            addProperty("name", c.name)
            addProperty("captured", true)
            addProperty("settled", chunksReady && lastTerrain != false)
            addProperty("stable", stable)
            addProperty("seconds", cameraSeconds)
            addProperty("loadSeconds", loadSeconds)
            addProperty("fps", frames / cameraSeconds)
        })
        log.info("Camera {} captured ({} s, load {} s, {} frames)", c.name, cameraSeconds, loadSeconds, frames)
    }

    private fun floatsJson(values: FloatArray) = JsonArray().also { a -> values.forEach { a.add(JsonPrimitive(it)) } }

    /** Every chunk of the scene box in range and every chunk the server has sent is on the client, none still queued. */
    private val chunksReady: Boolean get() = lastChunks[1] == 0 && lastChunks[3] == 0

    /**
     * [required, missing, out of range, view missing]: chunks overlapping the scene box within the server's and the
     * client's view distance, and the chunks the server has sent or still has queued for the player (the scene's
     * surroundings are drawn too) that the client does not have yet; view missing is -1 while the server's square is
     * not known or still the previous camera's.
     */
    private fun chunkStatus(mc: Minecraft): IntArray {
        val range = min(spec.viewDistance, mc.gameSettings.renderDistanceChunks)
        val view = serverView ?: return intArrayOf(0, 0, 0, -1)
        val pcx = view.centreX
        val pcz = view.centreZ
        // the square follows the player within 8 blocks: further off, it is still the previous camera's
        val c = camera
        if (max(abs(pcx - (c.x.toInt() shr 4)), abs(pcz - (c.z.toInt() shr 4))) > 1 || view.sent.isEmpty()) {
            return intArrayOf(0, 0, 0, -1)
        }
        val provider = mc.theWorld.getChunkProvider()
        var viewMissing = view.queued
        for (key in view.sent) {
            if (provider.provideChunk((key shr 32).toInt(), key.toInt()) is EmptyChunk) viewMissing++
        }
        val box = job.box ?: return intArrayOf(0, 0, 0, viewMissing)
        var required = 0
        var missing = 0
        var outOfRange = 0
        for (cx in (box[0] shr 4)..(box[3] shr 4)) {
            for (cz in (box[2] shr 4)..(box[5] shr 4)) {
                if (max(abs(cx - pcx), abs(cz - pcz)) > range) {
                    outOfRange++
                    continue
                }
                required++
                if (provider.provideChunk(cx, cz) is EmptyChunk) missing++
            }
        }
        return intArrayOf(required, missing, outOfRange, viewMissing)
    }

    /** Celeritas `isTerrainRenderComplete()`, else vanilla's pending chunk renderer list; null if neither is known. */
    private fun terrainComplete(mc: Minecraft): Boolean? {
        celeritas?.let { (instance, complete) ->
            val renderer = instance.invoke(null)
            if (renderer != null) {
                terrainSource = "celeritas"
                return complete.invoke(renderer) as Boolean
            }
        }
        val pending = worldRenderersToUpdate.get(mc.renderGlobal) as? List<*> ?: return null
        terrainSource = "vanilla"
        return pending.isEmpty()
    }
}
