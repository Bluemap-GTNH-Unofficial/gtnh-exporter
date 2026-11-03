package de.bluecolored.bluemap.gtnh.exporter.oracle

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.stream.JsonWriter
import com.gtnewhorizon.gtnhlib.client.renderer.TessellatorManager
import cpw.mods.fml.common.Loader
import cpw.mods.fml.common.registry.GameData
import cpw.mods.fml.relauncher.ReflectionHelper
import de.bluecolored.bluemap.gtnh.exporter.BlockExporter
import de.bluecolored.bluemap.gtnh.exporter.ExporterMod
import de.bluecolored.bluemap.gtnh.exporter.FakeWorld
import de.bluecolored.bluemap.gtnh.exporter.PackKeys
import de.bluecolored.bluemap.gtnh.exporter.PackWriter
import net.minecraft.block.Block
import net.minecraft.block.BlockLeaves
import net.minecraft.client.Minecraft
import net.minecraft.client.entity.EntityClientPlayerMP
import net.minecraft.client.network.NetHandlerPlayClient
import net.minecraft.client.renderer.EntityRenderer
import net.minecraft.client.renderer.OpenGlHelper
import net.minecraft.client.renderer.RenderBlocks
import net.minecraft.client.renderer.RenderHelper
import net.minecraft.client.renderer.Tessellator
import net.minecraft.client.renderer.entity.RenderManager
import net.minecraft.client.renderer.texture.DynamicTexture
import net.minecraft.client.renderer.texture.TextureAtlasSprite
import net.minecraft.client.renderer.texture.TextureMap
import net.minecraft.client.renderer.texture.TextureUtil
import net.minecraft.client.renderer.tileentity.TileEntityRendererDispatcher
import net.minecraft.client.resources.data.AnimationMetadataSection
import net.minecraft.client.shader.Framebuffer
import net.minecraft.entity.EntityLivingBase
import net.minecraft.entity.passive.EntityPig
import net.minecraft.nbt.CompressedStreamTools
import net.minecraft.nbt.NBTTagCompound
import net.minecraft.network.NetworkManager
import net.minecraft.stats.StatFileWriter
import net.minecraft.tileentity.TileEntity
import net.minecraftforge.client.ForgeHooksClient
import org.lwjgl.BufferUtils
import org.lwjgl.opengl.GL11
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.FloatBuffer
import java.util.Base64
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

/**
 * `states` mode: draws every listed block state alone in a [FakeWorld] the way the real client draws it, for the
 * per-state rendering oracle (`e2e oracle states`; camera and file conventions in [StateViews]):
 *
 * - the block with `RenderBlocks.renderBlockByRenderType` (GTNHLib-captured quads of render passes 0 and 1 as
 *   `canRenderInPass` allows, drawn with the terrain GL state of `EntityRenderer.renderWorld`: pass 0 opaque with
 *   alpha test 0.5, pass 1 blended without depth writes), then its tile entity's special renderer (passes 0/1 per
 *   `shouldRenderInPass`, standard item lighting, as `RenderGlobal.renderEntities`);
 * - capture conditions: full brightness (white lightmap), ambient occlusion off, fancy graphics, no fog, noon,
 *   plains, animated sprites pinned to their first animation frame, atlas sampled without mipmaps;
 * - each state is drawn three times into an offscreen framebuffer: over opaque black and over opaque white (the
 *   visual alpha is `1 - (white - black)`, [StateImages.recoverAlpha]), and once with depth writes in every pass
 *   for the face IDs ([StateImages.facesFromDepth]).
 *
 * Input: [statesFile], `<modid:name> <meta>` per line (the meta is the last token). Output in [outDir]:
 * `<ns>/<value>/<meta>.png` (RGBA, two views side by side), `<ns>/<value>/<meta>.faces.png` (face IDs) and
 * `index.json`.
 */
class StateCapture(private val mc: Minecraft, private val outDir: File, private val statesFile: File) {
    private class State(val name: String, val meta: Int) {
        val keyParts = PackKeys.blockKey(name)
        val key = "${keyParts.first}:${keyParts.second}[meta=$meta]"
        val path = "${keyParts.first}/${keyParts.second}/$meta"
        var block: Block? = null

        /** The meta placed in the world: [meta], except for GregTech machines (their base type, see [StateGregTech]). */
        var worldMeta = meta
        var tileEntity: TileEntity? = null
        var tesr = false
        var pass0: List<Quad> = emptyList()
        var pass1: List<Quad> = emptyList()
        val errors = LinkedHashSet<String>()
        var glError = 0
        var blank = true
        val info = JsonObject()
    }

    /** A captured quad in block-local coordinates (the block at the origin). */
    private class Quad(val x: FloatArray, val y: FloatArray, val z: FloatArray, val u: FloatArray, val v: FloatArray, val color: IntArray)

    private enum class Pass { BLACK, WHITE, DEPTH }

    private val world = FakeWorld.create()
    private val tessellatorDrawing = ReflectionHelper.findField(Tessellator::class.java, "isDrawing", "field_78415_z")

    fun run() {
        val started = System.nanoTime()
        val states = readStates()
        ExporterMod.log.info("Rendering {} block states to {}", states.size, outDir)
        outDir.mkdirs()
        world.setWorldTime(NOON)

        val settings = mc.gameSettings
        val previousAo = settings.ambientOcclusion
        val previousFancy = settings.fancyGraphics
        settings.ambientOcclusion = 0
        settings.fancyGraphics = true
        val writer = Executors.newFixedThreadPool(Runtime.getRuntime().availableProcessors().coerceIn(2, 16))
        val pending = ArrayList<Future<*>>()
        val framebuffer = Framebuffer(GRID_WIDTH, GRID_HEIGHT, true)
        val minFilter = prepareAtlas()
        whiteLightmap()
        val previousWorld = mc.theWorld
        val previousPlayer = mc.thePlayer
        val previousViewer = mc.renderViewEntity
        val clientWorld = try {
            StateWorldClient(mc, world).also { it.setWorldTime(NOON) }
        } catch (t: Throwable) {
            ExporterMod.log.warn("No client world for the renderers: {}", t.toString())
            null
        }
        val player = clientWorld?.let(::viewerPlayer)
        if (clientWorld != null) mc.theWorld = clientWorld
        if (player != null) {
            mc.thePlayer = player
            mc.renderViewEntity = player
        }
        prepareDispatchers(player)
        try {
            for ((batchIndex, batch) in states.chunked(CELLS).withIndex()) {
                for (state in batch) prepare(state)
                val depth = renderBatch(framebuffer, batch, Pass.DEPTH)
                val black = renderBatch(framebuffer, batch, Pass.BLACK)
                val white = renderBatch(framebuffer, batch, Pass.WHITE)
                val black8 = argbRows(black.first)
                val white8 = argbRows(white.first)
                val depthRows = depthRows(depth.second!!)
                for ((cell, state) in batch.withIndex()) {
                    val (argb, faces) = cellImages(cell, black8, white8, depthRows)
                    state.blank = argb.none { it ushr 24 != 0 }
                    pending += writer.submit {
                        StateImages.writeArgb(argb, File(outDir, "${state.path}.png"))
                        StateImages.writeFaces(faces, File(outDir, "${state.path}.faces.png"))
                    }
                    state.tileEntity = null
                    state.pass0 = emptyList()
                    state.pass1 = emptyList()
                }
                if ((batchIndex + 1) % 16 == 0) {
                    ExporterMod.log.info("Rendered {} / {} states", (batchIndex + 1) * CELLS, states.size)
                }
                pending.removeAll { it.isDone && run { it.get(); true } }
            }
            for (f in pending) f.get()
        } finally {
            writer.shutdown()
            writer.awaitTermination(10, TimeUnit.MINUTES)
            framebuffer.deleteFramebuffer()
            mc.entityRenderer.disableLightmap(0.0)
            mc.framebuffer.bindFramebuffer(true)
            restoreAtlas(minFilter)
            settings.ambientOcclusion = previousAo
            settings.fancyGraphics = previousFancy
            BlockExporter.setWorldRenderPass(-1)
            ForgeHooksClient.setRenderPass(-1)
            mc.theWorld = previousWorld
            mc.thePlayer = previousPlayer
            mc.renderViewEntity = previousViewer
        }
        writeIndex(states, (System.nanoTime() - started) / 1e9)
        ExporterMod.log.info("Rendered {} states in {} s", states.size, (System.nanoTime() - started) / 1e9)
    }

    private fun readStates(): List<State> {
        val states = ArrayList<State>()
        val seen = HashSet<String>()
        statesFile.forEachLine { line ->
            val trimmed = line.substringBefore('#').trim()
            if (trimmed.isEmpty()) return@forEachLine
            val split = trimmed.lastIndexOfAny(charArrayOf(' ', '\t'))
            require(split > 0) { "states line must be '<modid:name> <meta>': $line" }
            val name = trimmed.substring(0, split).trim()
            val meta = trimmed.substring(split + 1).toInt()
            if (seen.add("$name $meta")) states += State(name, meta)
        }
        return states
    }

    /** Looks the block up, creates its tile entity and captures its quads for both render passes. */
    private fun prepare(state: State) {
        val registry = GameData.getBlockRegistry()
        val block = if (registry.containsKey(state.name)) registry.getObject(state.name) else null
        if (block == null) {
            state.errors += "unknown block"
            return
        }
        state.block = block
        (block as? BlockLeaves)?.setGraphicsLevel(true)
        state.info.addProperty("renderType", block.renderType)
        state.info.addProperty("class", block.javaClass.name)
        val machine = state.name == StateGregTech.MACHINES && state.meta > 15 && Loader.isModLoaded("gregtech")
        val tileEntity = try {
            when {
                machine -> {
                    val (baseType, te) = StateGregTech.machine(state.meta) ?: error("no MTE ${state.meta}")
                    state.worldMeta = baseType
                    state.info.addProperty("worldMeta", baseType)
                    state.info.addProperty("mte", state.meta)
                    te
                }
                safe { block.hasTileEntity(state.meta) } == true -> block.createTileEntity(world, state.meta)
                else -> null
            }?.also { te ->
                te.xCoord = FakeWorld.X
                te.yCoord = FakeWorld.Y
                te.zCoord = FakeWorld.Z
                te.blockType = block
                te.blockMetadata = state.worldMeta
                te.setWorldObj(world)
            }
        } catch (t: Throwable) {
            state.errors += "tile entity: ${BlockExporter.reason(t)}"
            null
        }
        state.tileEntity = tileEntity
        if (tileEntity != null) {
            state.info.addProperty("tileEntity", tileEntity.javaClass.name)
            state.tesr = safe { TileEntityRendererDispatcher.instance.hasSpecialRenderer(tileEntity) } == true
            try {
                val tag = NBTTagCompound()
                tileEntity.writeToNBT(tag)
                val bytes = ByteArrayOutputStream()
                DataOutputStream(bytes).use { CompressedStreamTools.write(tag, it) }
                state.info.addProperty("te", Base64.getEncoder().encodeToString(bytes.toByteArray()))
            } catch (t: Throwable) {
                // the drawing is unaffected; only BlueMap does not get the tile entity data
                state.info.addProperty("teError", BlockExporter.reason(t))
            }
        }
        world.set(block, state.worldMeta, tileEntity)
        val passes = JsonArray()
        for (pass in 0..1) {
            if (safe { block.canRenderInPass(pass) } != true) continue
            passes.add(com.google.gson.JsonPrimitive(pass))
            val quads = try {
                capture(block, pass)
            } catch (t: Throwable) {
                state.errors += "render pass $pass: ${BlockExporter.reason(t)}"
                emptyList()
            }
            if (pass == 0) state.pass0 = quads else state.pass1 = quads
        }
        state.info.add("passes", passes)
        state.info.addProperty("quads", state.pass0.size + state.pass1.size)
    }

    private fun capture(block: Block, pass: Int): List<Quad> {
        val renderBlocks = RenderBlocks(world)
        BlockExporter.setWorldRenderPass(pass)
        val tessellator = TessellatorManager.startCapturingAndGet()
        var error: Throwable? = null
        try {
            tessellator.startDrawingQuads()
            renderBlocks.renderBlockByRenderType(block, FakeWorld.X, FakeWorld.Y, FakeWorld.Z)
        } catch (t: Throwable) {
            error = t
        }
        val result = ArrayList<Quad>()
        try {
            for (q in TessellatorManager.stopCapturingToPooledQuads()) {
                result += Quad(
                    FloatArray(4) { q.getX(it) - FakeWorld.X },
                    FloatArray(4) { q.getY(it) - FakeWorld.Y },
                    FloatArray(4) { q.getZ(it) - FakeWorld.Z },
                    FloatArray(4) { q.getTexU(it) },
                    FloatArray(4) { q.getTexV(it) },
                    IntArray(4) { q.getColor(it) },
                )
            }
        } finally {
            tessellator.clearQuads()
            BlockExporter.setWorldRenderPass(-1)
        }
        if (error != null) throw error
        return result
    }

    /** Draws every state of [batch] into its grid cell and reads the framebuffer back (colour, and depth for [Pass.DEPTH]). */
    private fun renderBatch(framebuffer: Framebuffer, batch: List<State>, pass: Pass): Pair<ByteBuffer, FloatBuffer?> {
        framebuffer.bindFramebuffer(true)
        val background = if (pass == Pass.WHITE) 1f else 0f
        GL11.glClearColor(background, background, background, 1f)
        GL11.glClearDepth(1.0)
        GL11.glDepthMask(true)
        GL11.glColorMask(true, true, true, true)
        GL11.glClear(GL11.GL_COLOR_BUFFER_BIT or GL11.GL_DEPTH_BUFFER_BIT)

        GL11.glMatrixMode(GL11.GL_PROJECTION)
        GL11.glPushMatrix()
        GL11.glLoadIdentity()
        GL11.glOrtho(-StateViews.HALF_EXTENT, StateViews.HALF_EXTENT, -StateViews.HALF_EXTENT, StateViews.HALF_EXTENT, -StateViews.DEPTH_RANGE, StateViews.DEPTH_RANGE)
        GL11.glMatrixMode(GL11.GL_MODELVIEW)
        GL11.glPushMatrix()
        try {
            for ((cell, state) in batch.withIndex()) {
                val block = state.block ?: continue
                world.set(block, state.worldMeta, state.tileEntity)
                val cellX = cell % COLUMNS * StateViews.WIDTH
                // framebuffer rows grow upwards: cell row 0 is the top row of the read-back image
                val cellY = GRID_HEIGHT - (cell / COLUMNS + 1) * StateViews.HEIGHT
                for (view in StateViews.YAWS.indices) {
                    GL11.glViewport(cellX + view * StateViews.VIEW, cellY, StateViews.VIEW, StateViews.VIEW)
                    GL11.glLoadIdentity()
                    GL11.glRotatef(StateViews.PITCH, 1f, 0f, 0f)
                    GL11.glRotatef(-StateViews.YAWS[view], 0f, 1f, 0f)
                    GL11.glTranslatef(-0.5f, -0.5f, -0.5f)
                    drawState(state, pass == Pass.DEPTH)
                }
                val error = GL11.glGetError()
                if (error != 0) state.glError = error
            }
        } finally {
            GL11.glViewport(0, 0, GRID_WIDTH, GRID_HEIGHT)
            GL11.glMatrixMode(GL11.GL_PROJECTION)
            GL11.glPopMatrix()
            GL11.glMatrixMode(GL11.GL_MODELVIEW)
            GL11.glPopMatrix()
        }
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1)
        val colour = BufferUtils.createByteBuffer(GRID_WIDTH * GRID_HEIGHT * 4)
        GL11.glReadPixels(0, 0, GRID_WIDTH, GRID_HEIGHT, GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, colour)
        var depth: FloatBuffer? = null
        if (pass == Pass.DEPTH) {
            depth = BufferUtils.createFloatBuffer(GRID_WIDTH * GRID_HEIGHT)
            GL11.glReadPixels(0, 0, GRID_WIDTH, GRID_HEIGHT, GL11.GL_DEPTH_COMPONENT, GL11.GL_FLOAT, depth)
        }
        return colour to depth
    }

    /** One view of one state: the GL state sequence of `EntityRenderer.renderWorld` for terrain and tile entities. */
    private fun drawState(state: State, depthPass: Boolean) {
        val tileEntity = state.tileEntity
        GL11.glEnable(GL11.GL_CULL_FACE)
        GL11.glEnable(GL11.GL_DEPTH_TEST)
        GL11.glDepthFunc(GL11.GL_LEQUAL)
        GL11.glEnable(GL11.GL_ALPHA_TEST)
        GL11.glAlphaFunc(GL11.GL_GREATER, 0.5f)
        GL11.glDisable(GL11.GL_FOG)
        GL11.glShadeModel(GL11.GL_FLAT)
        GL11.glDisable(GL11.GL_BLEND)
        GL11.glDepthMask(true)
        RenderHelper.disableStandardItemLighting()
        GL11.glColor4f(1f, 1f, 1f, 1f)

        // terrain pass 0
        terrainState()
        drawQuads(state.pass0)
        GL11.glAlphaFunc(GL11.GL_GREATER, 0.1f)
        if (tileEntity != null && state.tesr) drawTileEntity(state, tileEntity, 0)

        // terrain pass 1
        GL11.glDisable(GL11.GL_BLEND)
        GL11.glEnable(GL11.GL_CULL_FACE)
        OpenGlHelper.glBlendFunc(GL11.GL_SRC_ALPHA, GL11.GL_ONE_MINUS_SRC_ALPHA, GL11.GL_ONE, GL11.GL_ZERO)
        GL11.glAlphaFunc(GL11.GL_GREATER, 0.1f)
        GL11.glEnable(GL11.GL_BLEND)
        GL11.glDepthMask(depthPass)
        terrainState()
        drawQuads(state.pass1)
        GL11.glDisable(GL11.GL_BLEND)
        GL11.glShadeModel(GL11.GL_FLAT)
        if (tileEntity != null && state.tesr) drawTileEntity(state, tileEntity, 1)
        GL11.glDepthMask(true)
        GL11.glEnable(GL11.GL_CULL_FACE)
        GL11.glDisable(GL11.GL_BLEND)
    }

    private fun terrainState() {
        mc.textureManager.bindTexture(TextureMap.locationBlocksTexture)
        GL11.glEnable(GL11.GL_TEXTURE_2D)
        mc.entityRenderer.enableLightmap(0.0)
        OpenGlHelper.setLightmapTextureCoords(OpenGlHelper.lightmapTexUnit, 240f, 240f)
        GL11.glColor4f(1f, 1f, 1f, 1f)
    }

    private fun drawQuads(quads: List<Quad>) {
        if (quads.isEmpty()) return
        val tessellator = Tessellator.instance
        tessellator.startDrawingQuads()
        for (q in quads) {
            for (i in 0 until 4) {
                val c = q.color[i] // ABGR
                tessellator.setColorRGBA(c and 0xFF, c shr 8 and 0xFF, c shr 16 and 0xFF, c ushr 24)
                tessellator.addVertexWithUV(q.x[i].toDouble(), q.y[i].toDouble(), q.z[i].toDouble(), q.u[i].toDouble(), q.v[i].toDouble())
            }
        }
        tessellator.draw()
    }

    private fun drawTileEntity(state: State, tileEntity: TileEntity, pass: Int) {
        if (safe { tileEntity.shouldRenderInPass(pass) } != true) return
        ForgeHooksClient.setRenderPass(pass)
        RenderHelper.enableStandardItemLighting()
        mc.entityRenderer.enableLightmap(0.0)
        OpenGlHelper.setLightmapTextureCoords(OpenGlHelper.lightmapTexUnit, 240f, 240f)
        GL11.glColor4f(1f, 1f, 1f, 1f)
        val depth = GL11.glGetInteger(GL11.GL_MODELVIEW_STACK_DEPTH)
        GL11.glPushMatrix()
        try {
            TileEntityRendererDispatcher.instance.renderTileEntityAt(tileEntity, 0.0, 0.0, 0.0, 0f)
        } catch (t: Throwable) {
            state.errors += "tesr pass $pass: ${BlockExporter.reason(t.cause ?: t)}"
            if (tessellatorDrawing.getBoolean(Tessellator.instance)) {
                try {
                    Tessellator.instance.draw()
                } catch (_: Throwable) {
                    tessellatorDrawing.setBoolean(Tessellator.instance, false)
                }
            }
        } finally {
            var left = GL11.glGetInteger(GL11.GL_MODELVIEW_STACK_DEPTH)
            GL11.glMatrixMode(GL11.GL_MODELVIEW)
            while (left > depth && left < 64) {
                GL11.glPopMatrix()
                left--
            }
            RenderHelper.disableStandardItemLighting()
            ForgeHooksClient.setRenderPass(-1)
        }
    }

    /** Straight-alpha ARGB of the read-back image, top row first. */
    private fun argbRows(buffer: ByteBuffer): IntArray {
        val out = IntArray(GRID_WIDTH * GRID_HEIGHT)
        for (row in 0 until GRID_HEIGHT) {
            val src = (GRID_HEIGHT - 1 - row) * GRID_WIDTH * 4
            val dst = row * GRID_WIDTH
            for (x in 0 until GRID_WIDTH) {
                val i = src + x * 4
                val r = buffer.get(i).toInt() and 0xFF
                val g = buffer.get(i + 1).toInt() and 0xFF
                val b = buffer.get(i + 2).toInt() and 0xFF
                out[dst + x] = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
            }
        }
        return out
    }

    private fun depthRows(buffer: FloatBuffer): FloatArray {
        val out = FloatArray(GRID_WIDTH * GRID_HEIGHT)
        for (row in 0 until GRID_HEIGHT) {
            val src = (GRID_HEIGHT - 1 - row) * GRID_WIDTH
            for (x in 0 until GRID_WIDTH) out[row * GRID_WIDTH + x] = buffer.get(src + x)
        }
        return out
    }

    /** The RGBA and face-ID images of grid [cell]. */
    private fun cellImages(cell: Int, black: IntArray, white: IntArray, depth: FloatArray): Pair<IntArray, ByteArray> {
        val x0 = cell % COLUMNS * StateViews.WIDTH
        val y0 = cell / COLUMNS * StateViews.HEIGHT
        val size = StateViews.WIDTH * StateViews.HEIGHT
        val b = IntArray(size)
        val w = IntArray(size)
        val d = FloatArray(size)
        for (row in 0 until StateViews.HEIGHT) {
            val src = (y0 + row) * GRID_WIDTH + x0
            val dst = row * StateViews.WIDTH
            System.arraycopy(black, src, b, dst, StateViews.WIDTH)
            System.arraycopy(white, src, w, dst, StateViews.WIDTH)
            System.arraycopy(depth, src, d, dst, StateViews.WIDTH)
        }
        val argb = StateImages.recoverAlpha(b, w)
        val faces = ByteArray(size)
        for (view in StateViews.YAWS.indices) {
            StateImages.facesFromDepth(d, argb, view, StateViews.WIDTH, view * StateViews.VIEW, faces)
        }
        return argb to faces
    }

    /** Pins every animated block sprite to its first animation frame and disables mipmapped sampling; returns the old min filter. */
    private fun prepareAtlas(): Int {
        val atlas = mc.textureMapBlocks
        mc.textureManager.bindTexture(TextureMap.locationBlocksTexture)
        try {
            @Suppress("UNCHECKED_CAST")
            val animated = ReflectionHelper.getPrivateValue<List<TextureAtlasSprite>, TextureMap>(
                TextureMap::class.java, atlas, "listAnimatedSprites", "field_94258_i",
            )
            val frameCounter = ReflectionHelper.findField(TextureAtlasSprite::class.java, "frameCounter", "field_110973_g")
            val tickCounter = ReflectionHelper.findField(TextureAtlasSprite::class.java, "tickCounter", "field_110983_h")
            val metadata = ReflectionHelper.findField(TextureAtlasSprite::class.java, "animationMetadata", "field_110982_k")
            var pinned = 0
            for (sprite in animated) {
                try {
                    val meta = metadata.get(sprite) as? AnimationMetadataSection ?: continue
                    if (sprite.frameCount <= 1) continue
                    frameCounter.setInt(sprite, 0)
                    tickCounter.setInt(sprite, 0)
                    val frame = meta.getFrameIndex(0).coerceIn(0, sprite.frameCount - 1)
                    TextureUtil.uploadTextureMipmap(sprite.getFrameTextureData(frame), sprite.iconWidth, sprite.iconHeight, sprite.originX, sprite.originY, false, false)
                    pinned++
                } catch (t: Throwable) {
                    ExporterMod.log.warn("Could not pin the animation of {}: {}", sprite.iconName, t.toString())
                }
            }
            ExporterMod.log.info("Pinned {} of {} animated sprites to their first frame", pinned, animated.size)
        } catch (t: Throwable) {
            ExporterMod.log.warn("Could not pin animated sprites: {}", t.toString())
        }
        val minFilter = GL11.glGetTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MIN_FILTER)
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MIN_FILTER, GL11.GL_NEAREST)
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MAG_FILTER, GL11.GL_NEAREST)
        return minFilter
    }

    private fun restoreAtlas(minFilter: Int) {
        mc.textureManager.bindTexture(TextureMap.locationBlocksTexture)
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MIN_FILTER, minFilter)
    }

    /** Full brightness for every lightmap coordinate (the lightmap is never updated at the main menu). */
    private fun whiteLightmap() {
        val texture = ReflectionHelper.getPrivateValue<DynamicTexture, EntityRenderer>(
            EntityRenderer::class.java, mc.entityRenderer, "lightmapTexture", "field_78513_d",
        )
        texture.textureData.fill(-1)
        texture.updateDynamicTexture()
    }

    /**
     * `Minecraft.thePlayer` while drawing: a player without a connection standing near the camera (tile entity
     * renderers read its position and `ticksExisted`), or null if one cannot be built.
     */
    private fun viewerPlayer(clientWorld: StateWorldClient): EntityClientPlayerMP? = try {
        EntityClientPlayerMP(mc, clientWorld, mc.session, NetHandlerPlayClient(mc, null, NetworkManager(true)), StatFileWriter()).apply {
            setPosition(FakeWorld.X + VIEWER_OFFSET, FakeWorld.Y + VIEWER_OFFSET, FakeWorld.Z + VIEWER_OFFSET)
        }
    } catch (t: Throwable) {
        ExporterMod.log.warn("No player for the renderers: {}", t.toString())
        null
    }

    /** Tile entity / entity render dispatchers as `RenderGlobal.renderEntities` leaves them, with a viewer at the camera. */
    private fun prepareDispatchers(player: EntityClientPlayerMP?) {
        val viewer: EntityLivingBase? = player ?: try {
            EntityPig(world).apply { setPosition(FakeWorld.X + VIEWER_OFFSET, FakeWorld.Y + VIEWER_OFFSET, FakeWorld.Z + VIEWER_OFFSET) }
        } catch (t: Throwable) {
            ExporterMod.log.warn("No viewer entity for the tile entity renderers: {}", t.toString())
            null
        }
        val dispatcher = TileEntityRendererDispatcher.instance
        if (viewer != null) {
            dispatcher.cacheActiveRenderInfo(world, mc.textureManager, mc.fontRenderer, viewer, 0f)
            RenderManager.instance.cacheActiveRenderInfo(world, mc.textureManager, mc.fontRenderer, viewer, null, mc.gameSettings, 0f)
        } else {
            dispatcher.func_147543_a(world)
            dispatcher.field_147553_e = mc.textureManager
        }
        TileEntityRendererDispatcher.staticPlayerX = FakeWorld.X + VIEWER_OFFSET
        TileEntityRendererDispatcher.staticPlayerY = FakeWorld.Y + VIEWER_OFFSET
        TileEntityRendererDispatcher.staticPlayerZ = FakeWorld.Z + VIEWER_OFFSET
        RenderManager.renderPosX = FakeWorld.X + VIEWER_OFFSET
        RenderManager.renderPosY = FakeWorld.Y + VIEWER_OFFSET
        RenderManager.renderPosZ = FakeWorld.Z + VIEWER_OFFSET
    }

    private fun writeIndex(states: List<State>, seconds: Double) {
        // a shard of a parallel run (`gtnh-client states --parallel`) writes its index elsewhere; the launcher merges them
        val file = System.getProperty("bluemap.gtnh.exporter.statesIndex")?.let(::File) ?: File(outDir, "index.json")
        file.absoluteFile.parentFile.mkdirs()
        val partial = File(file.absoluteFile.parentFile, "${file.name}.part")
        JsonWriter(partial.bufferedWriter()).use { out ->
            out.setIndent(" ")
            out.beginObject()
            out.name("format").value(1)
            out.name("image")
            PackWriter.GSON.toJson(imageJson(), out)
            out.name("conditions").value(CONDITIONS)
            out.name("seconds").value(seconds)
            out.name("states").beginArray()
            for (state in states) {
                val json = JsonObject()
                json.addProperty("key", state.key)
                json.addProperty("name", state.name)
                json.addProperty("meta", state.meta)
                json.addProperty("path", state.path)
                for ((k, v) in state.info.entrySet()) json.add(k, v)
                json.addProperty("tesr", state.tesr)
                json.addProperty("blank", state.blank)
                if (state.glError != 0) json.addProperty("glError", state.glError)
                if (state.errors.isNotEmpty()) json.addProperty("error", state.errors.joinToString("; "))
                PackWriter.GSON.toJson(json, out)
            }
            out.endArray()
            out.endObject()
        }
        partial.renameTo(file)
    }

    private fun imageJson() = JsonObject().apply {
        addProperty("width", StateViews.WIDTH)
        addProperty("height", StateViews.HEIGHT)
        addProperty("viewSize", StateViews.VIEW)
        addProperty("halfExtent", StateViews.HALF_EXTENT)
        addProperty("depthRange", StateViews.DEPTH_RANGE)
        add("target", JsonArray().apply { repeat(3) { add(com.google.gson.JsonPrimitive(0.5)) } })
        add(
            "views",
            JsonArray().apply {
                for (i in StateViews.YAWS.indices) {
                    add(
                        JsonObject().apply {
                            addProperty("name", StateViews.NAMES[i])
                            addProperty("yaw", StateViews.YAWS[i])
                            addProperty("pitch", StateViews.PITCH)
                            addProperty("x", i * StateViews.VIEW)
                        },
                    )
                }
            },
        )
    }

    companion object {
        private const val COLUMNS = 8
        private const val ROWS = 8
        private const val CELLS = COLUMNS * ROWS
        private const val GRID_WIDTH = COLUMNS * StateViews.WIDTH
        private const val GRID_HEIGHT = ROWS * StateViews.HEIGHT
        private const val NOON = 6000L

        /** Viewer (player) position relative to the block, towards the cameras. */
        private const val VIEWER_OFFSET = 4.0
        private const val CONDITIONS =
            "real client, RenderBlocks.renderBlockByRenderType (captured passes 0/1) + TESR; FakeWorld at (0,64,0) with air " +
                "around, plains, noon; white lightmap (full brightness), ambient occlusion off, fancy graphics, no fog/clouds; " +
                "vanilla non-AO face shade as drawn by the renderers; atlas without mipmaps, animations at frame 0; " +
                "alpha test 0.5 (pass 0) / 0.1 (pass 1, TESR); visual alpha from black/white backgrounds"

        private inline fun <T> safe(block: () -> T): T? = try {
            block()
        } catch (_: Throwable) {
            null
        }
    }
}
