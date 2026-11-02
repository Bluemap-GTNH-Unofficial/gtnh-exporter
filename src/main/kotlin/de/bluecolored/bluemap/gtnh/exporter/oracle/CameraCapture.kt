package de.bluecolored.bluemap.gtnh.exporter.oracle

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
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
import net.minecraft.world.chunk.EmptyChunk
import net.minecraftforge.client.event.EntityViewRenderEvent
import net.minecraftforge.client.event.FOVUpdateEvent
import net.minecraftforge.client.event.RenderHandEvent
import net.minecraftforge.client.event.RenderWorldLastEvent
import net.minecraftforge.common.MinecraftForge
import org.lwjgl.BufferUtils
import org.lwjgl.opengl.GL11
import org.lwjgl.opengl.GL30
import java.awt.image.BufferedImage
import java.io.File
import java.lang.reflect.Field
import java.lang.reflect.Method
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.Collections
import java.util.IdentityHashMap
import java.util.Locale
import javax.imageio.ImageIO
import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min

/**
 * `-Dbluemap.gtnh.exporter.mode=cameras`: reference screenshots of a scene world for the world-scene oracle
 * (`e2e oracle world`). The client is started with `--server` pointing at a server that runs the scene world;
 * `gtnh-client cameras` sets everything up.
 *
 * Once in the world it switches the player to creative flight over RCON, then for every camera of the spec:
 * teleports the player there over RCON (`tp`), pins position and rotation on the client every tick and frame, waits
 * until the client has every chunk of the scene box in range and the chunk renderer reports no pending work, waits a
 * settle time, then until consecutive frames stop changing, and captures the next frame in [RenderWorldLastEvent]
 * (before other mods draw overlays, with the hand suppressed so the depth buffer stays intact): colour
 * (`<view>.png`), depth (`<view>.depth.bin`, float32 little endian, rows top to bottom, GL window depth 0..1) and the
 * projection and modelview matrices plus the camera position (`<view>.json`). `cameras-report.json` summarises the
 * run; then the client quits (exit 1 if any camera failed).
 *
 * Capture conditions (see the launcher README): no GUI, hand or block outline; flat lighting (smooth lighting off) with
 * a white lightmap (full brightness everywhere, vanilla's per-face shading 1.0 / 0.8 / 0.6 / 0.5 stays); no fog
 * (pushed beyond the far plane), no clouds, no view bobbing, minimal particles; noon and clear weather; FOV exactly
 * the camera's vertical FOV (the flying FOV boost is cancelled); animated block textures pinned to their first frame,
 * as the e2e captures of the BlueMap webapp do.
 *
 * System properties: `bluemap.gtnh.exporter.cameras` (spec JSON written by `gtnh-client cameras`),
 * `bluemap.gtnh.exporter.out` (output directory).
 */
object CameraCapture {
    private val log = ExporterMod.log

    private const val EYE_HEIGHT = 1.62 // EntityPlayer.yOffset: the client's posY is the eye, `tp` takes the feet
    private const val JOIN_SETTLE_TICKS = 40
    private const val FOV_SETTLE_TICKS = 30 // fovModifierHand eases towards 1.0 by half per tick
    private const val TELEPORT_TIMEOUT_SECONDS = 30.0
    private const val STABILIZE_TIMEOUT_SECONDS = 60.0
    private const val CHANGED_CHANNEL_DELTA = 8
    private const val FOG_DISTANCE = 1.0e6f

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

    private class Spec(
        val scene: String,
        val box: IntArray?,
        val cameras: List<Camera>,
        val rconHost: String,
        val rconPort: Int,
        val rconPasswordFile: File,
        val viewDistance: Int,
        val renderDistance: Int,
        val settleTicks: Int,
        val stableFrames: Int,
        val stableIntervalTicks: Int,
        val stableTolerance: Double,
        val joinTimeoutSeconds: Double,
        val cameraTimeoutSeconds: Double,
    )

    private enum class Phase { JOINING, SETUP, TELEPORT, SETTLE, STABILIZE, CAPTURE, FINISHED }

    private lateinit var spec: Spec
    private lateinit var outDir: File
    private var phase = Phase.JOINING
    private var phaseTicks = 0
    private var phaseStarted = 0L
    private var started = 0L
    private var joinedTicks = 0
    private var index = 0
    private var cameraStarted = 0L
    private var pinned: Camera? = null
    private var rcon: CameraRcon? = null
    private var playerName = ""

    // per camera
    private var readySince = -1
    private var settled = false
    private var settleSeconds = Double.NaN
    private var lastChunks = intArrayOf(0, 0, 0)
    private var lastTerrain: Boolean? = null
    private var snapshotRequested = false
    private var captureRequested = false
    private var lastSnapshot: ByteArray? = null
    private var stableCount = 0
    private var changes = mutableListOf<Double>()
    private var teleportReply = ""

    private val results = JsonArray()
    private var failures = 0
    private val frozenSprites: MutableSet<TextureAtlasSprite> = Collections.newSetFromMap(IdentityHashMap())

    private val lightmapTexture: Field = ReflectionHelper.findField(EntityRenderer::class.java, "lightmapTexture", "field_78513_d")
    private val lightmapUpdateNeeded: Field =
        ReflectionHelper.findField(EntityRenderer::class.java, "lightmapUpdateNeeded", "field_78536_aa")
    private val fovModifierHand: Field = ReflectionHelper.findField(EntityRenderer::class.java, "fovModifierHand", "field_78507_R")
    private val listAnimatedSprites: Field = ReflectionHelper.findField(TextureMap::class.java, "listAnimatedSprites", "field_94258_i")
    private val frameCounter: Field = ReflectionHelper.findField(TextureAtlasSprite::class.java, "frameCounter", "field_110973_g")
    private val tickCounter: Field = ReflectionHelper.findField(TextureAtlasSprite::class.java, "tickCounter", "field_110983_h")
    private val animationMetadata: Field =
        ReflectionHelper.findField(TextureAtlasSprite::class.java, "animationMetadata", "field_110982_k")
    private val ariModelview: Field = ReflectionHelper.findField(ActiveRenderInfo::class.java, "modelview", "field_74594_j")
    private val ariProjection: Field = ReflectionHelper.findField(ActiveRenderInfo::class.java, "projection", "field_74595_k")
    private val worldRenderersToUpdate: Field =
        ReflectionHelper.findField(RenderGlobal::class.java, "worldRenderersToUpdate", "field_72767_j")

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
        outDir = File(System.getProperty("bluemap.gtnh.exporter.out") ?: File(mc.mcDataDir, "bluemap-gtnh-cameras").path).absoluteFile
        outDir.mkdirs()
        applySettings(mc)
        started = System.nanoTime()
        phaseStarted = started
        FMLCommonHandler.instance().bus().register(this)
        MinecraftForge.EVENT_BUS.register(this)
        log.info("Camera mode: {} cameras of scene {} -> {}", spec.cameras.size, spec.scene, outDir)
    }

    private fun readSpec(file: File): Spec {
        val json = JsonParser().parse(file.readText(Charsets.UTF_8)).asJsonObject
        val rcon = json.getAsJsonObject("rcon")
        val cameras = json.getAsJsonArray("cameras").map { element ->
            val c = element.asJsonObject
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
        require(cameras.isNotEmpty()) { "no cameras in $file" }
        fun int(key: String, default: Int) = json.get(key)?.asInt ?: default
        fun double(key: String, default: Double) = json.get(key)?.asDouble ?: default
        return Spec(
            scene = json.get("scene").asString,
            box = json.getAsJsonArray("box")?.map { it.asInt }?.toIntArray(),
            cameras = cameras,
            rconHost = rcon.get("host").asString,
            rconPort = rcon.get("port").asInt,
            rconPasswordFile = File(rcon.get("passwordFile").asString),
            viewDistance = int("viewDistance", 8),
            renderDistance = int("renderDistance", 12),
            settleTicks = int("settleTicks", 60),
            stableFrames = int("stableFrames", 3),
            stableIntervalTicks = int("stableIntervalTicks", 10),
            stableTolerance = double("stableTolerance", 0.0005),
            joinTimeoutSeconds = double("joinTimeoutSeconds", 900.0),
            cameraTimeoutSeconds = double("cameraTimeoutSeconds", 240.0),
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
        addProperty("terrainRenderer", terrainSource)
        Loader.instance().indexedModList["angelica"]?.let { addProperty("angelica", it.version) }
    }

    private fun enter(next: Phase) {
        phase = next
        phaseTicks = 0
        phaseStarted = System.nanoTime()
    }

    private fun seconds(since: Long) = (System.nanoTime() - since) / 1e9

    private val camera: Camera get() = spec.cameras[index]

    private fun rconCommand(command: String): String {
        val client = rcon ?: CameraRcon(spec.rconHost, spec.rconPort, spec.rconPasswordFile.readText().trim()).also { rcon = it }
        val reply = client.command(command)
        log.info("RCON {} -> {}", command, reply.trim())
        return reply
    }

    private fun fmt(v: Double) = String.format(Locale.ROOT, "%.4f", v) // a '.' keeps `tp` from adding 0.5

    @SubscribeEvent
    fun onClientTick(event: TickEvent.ClientTickEvent) {
        if (phase == Phase.FINISHED) return
        val mc = Minecraft.getMinecraft()
        try {
            if (event.phase == TickEvent.Phase.START) {
                pin(mc)
                return
            }
            pin(mc)
            phaseTicks++
            step(mc)
            if (phase > Phase.SETUP) ensureSize(mc, rebind = false)
        } catch (t: Throwable) {
            log.fatal("Camera capture failed", t)
            finish(mc, "${t.javaClass.simpleName}: ${t.message}")
        }
    }

    private fun step(mc: Minecraft) {
        when (phase) {
            Phase.JOINING -> {
                val screen = mc.currentScreen
                if (screen is GuiDisconnected) throw IllegalStateException("disconnected from the server")
                if (mc.theWorld != null && mc.thePlayer != null && screen !is GuiDownloadTerrain) {
                    if (++joinedTicks >= JOIN_SETTLE_TICKS) {
                        playerName = mc.thePlayer.commandSenderName
                        enter(Phase.SETUP)
                    }
                } else {
                    joinedTicks = 0
                }
                if (seconds(started) > spec.joinTimeoutSeconds) {
                    throw IllegalStateException("not in the world after ${spec.joinTimeoutSeconds} s (screen ${screen?.javaClass?.name})")
                }
            }
            Phase.SETUP -> {
                if (phaseTicks == 1) {
                    rconCommand("gamemode 1 $playerName")
                    rconCommand("time set 6000")
                    rconCommand("weather clear 1000000")
                    rconCommand("gamerule doDaylightCycle false")
                }
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
                    enter(Phase.SETTLE)
                } else if (seconds(phaseStarted) > TELEPORT_TIMEOUT_SECONDS) {
                    cameraFailed(mc, "teleport not confirmed by the server (reply: ${teleportReply.trim()})")
                }
            }
            Phase.SETTLE -> {
                lastChunks = chunkStatus(mc)
                lastTerrain = terrainComplete(mc)
                val ready = lastChunks[1] == 0 && lastTerrain != false && phaseTicks >= FOV_SETTLE_TICKS
                if (!ready) {
                    readySince = -1
                } else if (readySince < 0) {
                    readySince = phaseTicks
                }
                if (readySince >= 0 && phaseTicks - readySince >= spec.settleTicks) {
                    settled = true
                    settleSeconds = seconds(cameraStarted)
                    enter(Phase.STABILIZE)
                } else if (seconds(cameraStarted) > spec.cameraTimeoutSeconds) {
                    log.warn("Camera {}: not settled after {} s (chunks {}, terrain complete {})", camera.name,
                        spec.cameraTimeoutSeconds, lastChunks.contentToString(), lastTerrain)
                    settleSeconds = seconds(cameraStarted)
                    enter(Phase.STABILIZE)
                }
            }
            Phase.STABILIZE -> {
                lastTerrain = terrainComplete(mc)
                if (phaseTicks % spec.stableIntervalTicks == 0) snapshotRequested = true
                if (stableCount >= spec.stableFrames - 1 && lastTerrain != false) {
                    enter(Phase.CAPTURE)
                    captureRequested = true
                } else if (seconds(phaseStarted) > STABILIZE_TIMEOUT_SECONDS) {
                    log.warn("Camera {}: frames still changing after {} s ({})", camera.name, STABILIZE_TIMEOUT_SECONDS, changes)
                    enter(Phase.CAPTURE)
                    captureRequested = true
                }
            }
            Phase.CAPTURE -> {
                // the capture itself happens in the next RenderWorldLastEvent of the right size
                if (!captureRequested) nextCamera(mc)
                else if (seconds(phaseStarted) > TELEPORT_TIMEOUT_SECONDS) cameraFailed(mc, "no frame of the camera size was rendered")
            }
            Phase.FINISHED -> {}
        }
    }

    private fun startCamera(mc: Minecraft) {
        val c = camera
        pinned = null
        mc.gameSettings.fovSetting = c.fov
        cameraStarted = System.nanoTime()
        readySince = -1
        settled = false
        settleSeconds = Double.NaN
        snapshotRequested = false
        captureRequested = false
        lastSnapshot = null
        stableCount = 0
        changes = mutableListOf()
        teleportReply = rconCommand("tp $playerName ${fmt(c.x)} ${fmt(c.y - EYE_HEIGHT)} ${fmt(c.z)}")
        log.info("Camera {} ({}/{}): eye {} {} {}, yaw {}, pitch {}, fov {}", c.name, index + 1, spec.cameras.size, c.x, c.y, c.z,
            c.yaw, c.pitch, c.fov)
        enter(Phase.TELEPORT)
    }

    private fun nextCamera(mc: Minecraft) {
        index++
        if (index < spec.cameras.size) startCamera(mc) else finish(mc, null)
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
        snapshotRequested = false
        nextCamera(mc)
    }

    private fun finish(mc: Minecraft, error: String?) {
        if (phase == Phase.FINISHED) return
        phase = Phase.FINISHED
        pinned = null
        val report = JsonObject().apply {
            addProperty("scene", spec.scene)
            addProperty("cameras", spec.cameras.size)
            addProperty("captured", results.count { it.asJsonObject.get("captured").asBoolean })
            addProperty("failed", spec.cameras.size - results.count { it.asJsonObject.get("captured").asBoolean })
            error?.let { addProperty("error", it) }
            addProperty("seconds", seconds(started))
            add("settings", settingsJson(mc))
            add("results", results)
        }
        File(outDir, "cameras-report.json").writeText(PackWriter.PRETTY_GSON.toJson(report))
        try {
            rcon?.close()
        } catch (_: Exception) {
        }
        if (System.getProperty("bluemap.gtnh.exporter.stay") == "true") return
        if (error == null && failures == 0) {
            log.info("Camera capture finished, shutting down the client")
            mc.shutdown()
        } else {
            log.error("Camera capture finished with errors ({}), exiting with 1", error ?: "$failures cameras failed")
            FMLCommonHandler.instance().exitJava(1, false)
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
        val c = pinned ?: spec.cameras.getOrNull(index) ?: return
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
        if (event.phase != TickEvent.Phase.START || phase <= Phase.JOINING || phase == Phase.FINISHED) return
        val mc = Minecraft.getMinecraft()
        if (mc.theWorld == null || mc.thePlayer == null) return
        try {
            pin(mc)
            ensureSize(mc, rebind = true)
            if (mc.currentScreen != null) mc.displayGuiScreen(null)
            mc.gameSettings.hideGUI = true
            freezeAnimations(mc)
            whiteLightmap(mc)
        } catch (t: Throwable) {
            log.fatal("Camera capture failed", t)
            finish(mc, "${t.javaClass.simpleName}: ${t.message}")
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
        if (phase > Phase.JOINING) event.isCanceled = true
    }

    @SubscribeEvent
    fun onFog(event: EntityViewRenderEvent.RenderFogEvent) {
        if (phase <= Phase.JOINING) return
        GL11.glFogf(GL11.GL_FOG_START, FOG_DISTANCE)
        GL11.glFogf(GL11.GL_FOG_END, FOG_DISTANCE * 2)
    }

    @SubscribeEvent
    fun onFov(event: FOVUpdateEvent) {
        // no FOV boost from flying or speed: the projection uses exactly gameSettings.fovSetting
        if (phase > Phase.JOINING) event.newfov = 1.0f
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
        if (!snapshotRequested && !captureRequested) return
        val mc = Minecraft.getMinecraft()
        val c = pinned ?: return
        try {
            val viewport = BufferUtils.createIntBuffer(16)
            GL11.glGetInteger(GL11.GL_VIEWPORT, viewport)
            if (mc.displayWidth != c.width || mc.displayHeight != c.height || viewport.get(2) != c.width || viewport.get(3) != c.height) return
            val pixels = readColor(c.width, c.height)
            val bytes = ByteArray(pixels.remaining()).also { pixels.get(it) }
            if (captureRequested) {
                capture(mc, c, event.partialTicks, bytes, viewport)
                captureRequested = false
                snapshotRequested = false
            } else {
                val previous = lastSnapshot
                if (previous != null) {
                    val changed = changedRatio(previous, bytes)
                    changes.add(changed)
                    stableCount = if (changed <= spec.stableTolerance) stableCount + 1 else 0
                }
                lastSnapshot = bytes
                snapshotRequested = false
            }
        } catch (t: Throwable) {
            log.fatal("Camera capture failed", t)
            finish(mc, "${t.javaClass.simpleName}: ${t.message}")
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
        val json = JsonObject().apply {
            addProperty("name", c.name)
            addProperty("scene", spec.scene)
            addProperty("width", w)
            addProperty("height", h)
            add("eye", JsonArray().also { a -> doubleArrayOf(c.x, c.y, c.z).forEach { a.add(com.google.gson.JsonPrimitive(it)) } })
            addProperty("yaw", c.yaw)
            addProperty("pitch", c.pitch)
            addProperty("fov", c.fov)
            add("camera", JsonArray().also { a -> cameraPos.forEach { a.add(com.google.gson.JsonPrimitive(it)) } })
            add("renderPos", JsonArray().also { a ->
                doubleArrayOf(RenderManager.renderPosX, RenderManager.renderPosY, RenderManager.renderPosZ)
                    .forEach { a.add(com.google.gson.JsonPrimitive(it)) }
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
            add("viewport", JsonArray().also { a -> (0 until 4).forEach { a.add(com.google.gson.JsonPrimitive(viewport.get(it))) } })
            addProperty("framebufferBinding", framebuffer)
            addProperty("gameFramebuffer", mc.framebuffer.framebufferObject)
            addProperty("image", "${c.name}.png")
            add("depth", JsonObject().apply {
                addProperty("file", "${c.name}.depth.bin")
                addProperty("format", "float32 little endian, rows top to bottom, OpenGL window depth 0..1 (1 = nothing drawn)")
            })
            add("settle", JsonObject().apply {
                addProperty("settled", settled)
                addProperty("seconds", settleSeconds)
                addProperty("chunksRequired", lastChunks[0])
                addProperty("chunksMissing", lastChunks[1])
                addProperty("chunksOutOfRange", lastChunks[2])
                lastTerrain?.let { addProperty("terrainComplete", it) }
                addProperty("terrainRenderer", terrainSource)
            })
            add("stability", JsonObject().apply {
                addProperty("stable", stableCount >= spec.stableFrames - 1)
                addProperty("tolerance", spec.stableTolerance)
                add("changedRatios", JsonArray().also { a -> changes.forEach { a.add(com.google.gson.JsonPrimitive(it)) } })
            })
            addProperty("teleport", teleportReply.trim())
            addProperty("seconds", seconds(cameraStarted))
        }
        File(outDir, "${c.name}.json").writeText(PackWriter.PRETTY_GSON.toJson(json))
        results.add(JsonObject().apply {
            addProperty("name", c.name)
            addProperty("captured", true)
            addProperty("settled", settled)
            addProperty("stable", stableCount >= spec.stableFrames - 1)
            addProperty("seconds", seconds(cameraStarted))
        })
        log.info("Camera {} captured ({} s)", c.name, seconds(cameraStarted))
    }

    private fun floatsJson(values: FloatArray) = JsonArray().also { a -> values.forEach { a.add(com.google.gson.JsonPrimitive(it)) } }

    /** [required, missing, out of range]: chunks overlapping the scene box within the server's and the client's view distance. */
    private fun chunkStatus(mc: Minecraft): IntArray {
        val box = spec.box ?: return intArrayOf(0, 0, 0)
        val c = camera
        val range = min(spec.viewDistance, mc.gameSettings.renderDistanceChunks)
        val pcx = floor(c.x / 16).toInt()
        val pcz = floor(c.z / 16).toInt()
        var required = 0
        var missing = 0
        var outOfRange = 0
        val provider = mc.theWorld.getChunkProvider()
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
        return intArrayOf(required, missing, outOfRange)
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
