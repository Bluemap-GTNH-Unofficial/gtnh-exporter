package de.bluecolored.bluemap.gtnh.exporter

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.gtnewhorizon.gtnhlib.client.renderer.TessellatorManager
import cpw.mods.fml.common.registry.GameData
import net.minecraft.block.Block
import net.minecraft.block.BlockBreakable
import net.minecraft.block.BlockFence
import net.minecraft.block.BlockLeaves
import net.minecraft.block.BlockPane
import net.minecraft.client.Minecraft
import net.minecraft.client.renderer.RenderBlocks
import net.minecraft.client.renderer.tileentity.TileEntityRendererDispatcher
import net.minecraft.init.Blocks
import net.minecraft.item.Item
import net.minecraft.item.ItemStack
import net.minecraft.tileentity.TileEntity
import net.minecraft.util.IIcon
import net.minecraft.world.biome.BiomeGenBase
import net.minecraftforge.client.ForgeHooksClient
import net.minecraftforge.common.util.ForgeDirection
import java.io.File
import java.util.TreeMap
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * Exports every registered non-vanilla block × meta: JSON cube models for standard cubes, captured quads
 * (`gtnh:quads`) for everything else, blockstates `meta=<n>`, block properties and block colors.
 */
class BlockExporter(
    private val mc: Minecraft,
    private val pack: PackWriter,
    private val textures: TextureExporter,
    private val extraStates: Map<String, Set<Int>>,
    private val modData: ModData,
) {
    private val world = FakeWorld.create()
    private val renderBlocks = RenderBlocks(world)

    /** modid -> statistics. */
    val mods = TreeMap<String, ModStats>()

    class ModStats {
        var blocks = 0
        var states = 0
        var cube = 0
        var quads = 0
        /** States rendered by the bounding-box fallback (counted in [cube] too). */
        var bbox = 0
        var failed = 0
        val failedReasons = TreeMap<String, Int>()
        val failedStates = ArrayList<String>()

        fun toJson() = JsonObject().apply {
            addProperty("blocks", blocks)
            addProperty("states", states)
            addProperty("cube", cube)
            addProperty("quads", quads)
            addProperty("bbox", bbox)
            addProperty("failed", failed)
            add("failedReasons", JsonObject().also { o -> failedReasons.forEach { (k, v) -> o.addProperty(k, v) } })
            add("failedStates", JsonArray().also { a -> failedStates.take(MAX_LISTED_FAILURES).forEach(a::addString) })
        }
    }

    private val blockProperties = TreeMap<String, JsonObject>() // namespace -> {state: props}
    private val blockColors = TreeMap<String, JsonObject>() // namespace -> {state: color}
    private val blocksNdjson = StringBuilder()
    private val shapes = JsonObject() // "<ns>:<value>" -> {"*": shape, "<meta>": shape}
    private val blockShapes = HashMap<Int, JsonObject>() // meta -> shape of the block being exported
    private val blockBounds = HashMap<Int, DoubleArray>() // meta -> bounds of the block being exported
    private val modelTextures = HashMap<String, Collection<String>>() // model key -> texture keys it uses

    var unmappedQuads = 0
        private set

    private sealed class Tint {
        object None : Tint()
        data class Fixed(val rgb: Int) : Tint()
        data class Biome(val calculator: String, val captureRgb: Int) : Tint()
        data class Unmatched(val captureRgb: Int) : Tint()
    }

    fun exportAll() {
        val registry = GameData.getBlockRegistry()
        val previousAo = mc.gameSettings.ambientOcclusion
        val previousFancy = mc.gameSettings.fancyGraphics
        mc.gameSettings.ambientOcclusion = 0
        mc.gameSettings.fancyGraphics = true
        try {
            for (block in registry.typeSafeIterable()) {
                val name = registry.getNameForObject(block) ?: continue
                if (name.startsWith("minecraft:")) continue
                try {
                    exportBlock(name, block)
                } catch (t: Throwable) {
                    ExporterMod.log.error("Exporting block {} failed", name, t)
                    val stats = mods.getOrPut(modid(name)) { ModStats() }
                    stats.blocks++
                    stats.states++
                    stats.failed++
                    stats.failedReasons.merge("block: ${reason(t)}", 1, Int::plus)
                    stats.failedStates.add("$name: ${reason(t)}")
                }
            }
        } finally {
            mc.gameSettings.ambientOcclusion = previousAo
            mc.gameSettings.fancyGraphics = previousFancy
            setWorldRenderPass(-1)
        }

        for ((namespace, json) in blockProperties) pack.json("assets/$namespace/blockProperties.json", json)
        for ((namespace, json) in blockColors) pack.json("assets/$namespace/blockColors.json", json)
        pack.bytes("gtnh/blocks.ndjson", blocksNdjson.toString().toByteArray(Charsets.UTF_8))
        pack.json(
            "data/gtnh/block_shapes.json",
            JsonObject().apply {
                addProperty("format", 1)
                add("blocks", shapes)
            },
        )
    }

    /**
     * Collision/connection shape of the block alone in the fake world, for the legacy reader's neighbour fixers
     * (vanilla fences, panes, walls, redstone next to modded blocks): `data/gtnh/block_shapes.json`.
     */
    private fun shapeOf(block: Block): JsonObject {
        val x = FakeWorld.X
        val y = FakeWorld.Y
        val z = FakeWorld.Z
        var solidSides = 0
        for (side in 0 until 6) {
            if (safe { block.isSideSolid(world, x, y, z, ForgeDirection.getOrientation(side)) } == true) solidSides = solidSides or (1 shl side)
        }
        return JsonObject().apply {
            addProperty("fullCube", safe { block.renderAsNormalBlock() } ?: false)
            addProperty("opaque", safe { block.material.isOpaque && block.isOpaqueCube } ?: false)
            addProperty("solidSides", solidSides)
            addProperty("fenceConnect", safe { (Blocks.fence as BlockFence).canConnectFenceTo(world, x, y, z) } ?: false)
            addProperty("paneConnect", safe { (Blocks.iron_bars as BlockPane).canPaneConnectToBlock(block) } ?: false)
        }
    }

    private fun addShapes(key: String) {
        if (blockShapes.isEmpty()) return
        val default = blockShapes[0] ?: blockShapes.values.first()
        val json = JsonObject().apply { add("*", default) }
        for ((meta, shape) in blockShapes.toSortedMap()) {
            if (shape != default) json.add(meta.toString(), shape)
        }
        shapes.add(key, json)
        blockShapes.clear()
    }

    private fun modid(name: String) = name.substringBefore(':')

    private fun metasOf(name: String, block: Block): Set<Int> {
        val metas = java.util.TreeSet<Int>()
        for (m in 0..15) metas.add(m)
        val item = Item.getItemFromBlock(block)
        if (item != null) {
            try {
                val list = ArrayList<ItemStack>()
                block.getSubBlocks(item, block.creativeTabToDisplayOn, list)
                for (stack in list) {
                    val damage = stack.itemDamage
                    val meta = item.getMetadata(damage)
                    if (meta in 0..65535 && meta != 32767) metas.add(meta)
                    if (item.hasSubtypes && damage in 0..65535 && damage != 32767) metas.add(damage)
                }
            } catch (t: Throwable) {
                ExporterMod.log.debug("getSubBlocks of {} failed: {}", name, t.toString())
            }
        }
        extraStates[name]?.let(metas::addAll)
        return metas
    }

    private fun exportBlock(name: String, block: Block) {
        blockShapes.clear()
        (block as? BlockLeaves)?.setGraphicsLevel(true)
        val (namespace, value) = PackKeys.blockKey(name)
        val stats = mods.getOrPut(modid(name)) { ModStats() }
        stats.blocks++

        val renderType = block.renderType
        val opaque = block.isOpaqueCube
        val info = JsonObject().apply {
            addProperty("name", name)
            addProperty("key", "$namespace:$value")
            addProperty("class", block.javaClass.name)
            addProperty("renderType", renderType)
            addProperty("opaque", opaque)
            addProperty("normal", safe { block.renderAsNormalBlock() })
            addProperty("pass", safe { block.renderBlockPass })
            addProperty("light", block.lightValue)
        }
        val metaInfos = JsonArray()

        val variants = LinkedHashMap<Int, JsonObject>()
        val models = HashMap<String, String>() // content signature -> model key
        val metas = metasOf(name, block)
        val explicitMetas = HashSet<Int>().also { set -> extraStates[name]?.let(set::addAll) }
        // failed metas that exist in worlds (0..15 or listed as extra states), for RENDERER_OVERRIDES blocks
        val failedInWorld = java.util.TreeSet<Int>()
        // metas whose tile entity has a dedicated TESR port (`ModData.rendererForTileEntity`)
        val tileEntityRenderers = HashMap<Int, String>()

        for (meta in metas) {
            val metaInfo = JsonObject().apply { addProperty("meta", meta) }
            val result = try {
                exportState(name, block, meta, renderType, namespace, value, models, metaInfo, meta <= 15 && meta !in explicitMetas)
            } catch (t: Throwable) {
                StateResult.Failed("exception: ${reason(t)}")
            }
            when (result) {
                is StateResult.Invalid -> {
                    blockShapes.remove(meta)
                    continue
                }
                is StateResult.Model -> {
                    variants[meta] = JsonObject().apply {
                        if (result.quads) addProperty("renderer", "gtnh:quads")
                        addProperty("model", result.model)
                    }
                    if (result.quads) stats.quads++ else stats.cube++
                    if (result.bbox) stats.bbox++
                    metaInfo.addProperty("result", if (result.quads) "quads" else if (result.bbox) "bbox" else "cube")
                    metaInfo.addProperty("model", result.model)
                    result.color?.let { color ->
                        blockColors.getOrPut(namespace) { JsonObject() }.addProperty("$namespace:$value[meta=$meta]", color)
                    }
                }
                is StateResult.Failed -> {
                    stats.failed++
                    stats.failedReasons.merge(result.reason.substringBefore(':'), 1, Int::plus)
                    stats.failedStates.add("$name:$meta ${result.reason}")
                    metaInfo.addProperty("result", "failed")
                    metaInfo.addProperty("reason", result.reason)
                    if (meta <= 15 || meta in explicitMetas) failedInWorld.add(meta)
                }
            }
            metaInfo.get("tileEntity")?.asString?.let(ModData::rendererForTileEntity)?.let { tileEntityRenderers[meta] = it }
            stats.states++
            metaInfos.add(metaInfo)
        }
        info.add("metas", metaInfos)
        blocksNdjson.append(PackWriter.GSON.toJson(info)).append('\n')
        addShapes("$namespace:$value")

        // blocks drawn by a dedicated renderer (GregTech, Phase 4; mod renderers in gtnh-render `render/mods`) get a
        // variant for every meta, even those whose static appearance failed: the renderer draws from tile-entity/meta
        // data and only falls back to the model
        // full cube = drawn by Chisel's CTM renderer on full bounds (stairs, glow, eldritch, waterstone use other
        // render types and keep their captured quads)
        val ctmRenderType = block.renderType == ModData.chiselCtmRenderId()
        val chisel = modData.chisel(block, (variants.keys + failedInWorld).toSortedSet()) { meta ->
            ctmRenderType && blockBounds[meta]?.contentEquals(FULL_BOUNDS) == true
        }
        modData.addChisel("$namespace:$value", chisel)
        val blockRenderer = RENDERER_OVERRIDES[name] ?: ModData.rendererFor(name)
        for (meta in (variants.keys + failedInWorld).toSortedSet()) {
            val renderer = blockRenderer
                ?: tileEntityRenderers[meta]
                ?: (if (chisel[meta]?.get("fullCube")?.asBoolean == true) "gtnh:chisel_ctm" else null)
                ?: continue
            val variant = variants.getOrPut(meta) { JsonObject().apply { addProperty("model", "bluemap:block/missing") } }
            variant.addProperty("renderer", renderer)
        }
        blockBounds.clear()
        // MyCTMLib connected textures (Modernity): `gtnh:ctm` on variants that use such a texture and have no
        // dedicated renderer (captured quads included; the model stays the fallback)
        if (textures.myCtm.isNotEmpty()) {
            for (variant in variants.values) {
                val renderer = variant.get("renderer")?.asString
                if (renderer != null && renderer != "gtnh:quads") continue
                val used = modelTextures[variant.get("model").asString] ?: continue
                if (used.any { it in textures.myCtm }) variant.addProperty("renderer", "gtnh:ctm")
            }
        }
        if (variants.isNotEmpty()) {
            val variantsJson = JsonObject()
            // the first variant is BlueMap's fallback for metas without a variant
            for ((meta, variant) in variants) variantsJson.add("meta=$meta", variant)
            pack.json("assets/$namespace/blockstates/$value.json", JsonObject().apply { add("variants", variantsJson) })
        }

        blockProperties.getOrPut(namespace) { JsonObject() }.add(
            "$namespace:$value",
            JsonObject().apply {
                addProperty("culling", opaque)
                addProperty("occluding", opaque)
                if (block is BlockBreakable) addProperty("cullingIdentical", true)
            },
        )
    }

    private sealed class StateResult {
        /** Meta that the block does not support (only for probed metas). */
        object Invalid : StateResult()

        class Model(val model: String, val quads: Boolean, val color: String?, val bbox: Boolean = false) : StateResult()

        class Failed(val reason: String) : StateResult()
    }

    private fun exportState(
        name: String,
        block: Block,
        meta: Int,
        renderType: Int,
        namespace: String,
        value: String,
        models: HashMap<String, String>,
        info: JsonObject,
        probed: Boolean,
    ): StateResult {
        val itemIcons = (0 until 6).map { side -> safe { block.getIcon(side, meta) }?.takeUnless { TextureExporter.isErrorIcon(it.iconName) } }
        if (probed && meta != 0 && itemIcons.all { it == null } && renderType == 0) return StateResult.Invalid

        val tileEntity = if (safe { block.hasTileEntity(meta) } == true) {
            try {
                block.createTileEntity(world, meta)?.also { te ->
                    te.xCoord = FakeWorld.X
                    te.yCoord = FakeWorld.Y
                    te.zCoord = FakeWorld.Z
                    te.blockType = block
                    te.blockMetadata = meta
                    te.setWorldObj(world)
                }
            } catch (t: Throwable) {
                info.addProperty("tileEntityError", reason(t))
                null
            }
        } else {
            null
        }
        world.set(block, meta, tileEntity)
        world.biome = BiomeGenBase.plains
        tileEntity?.let { info.addProperty("tileEntity", it.javaClass.name) }

        safe { block.setBlockBoundsBasedOnState(world, FakeWorld.X, FakeWorld.Y, FakeWorld.Z) }
        val bounds = doubleArrayOf(
            block.blockBoundsMinX, block.blockBoundsMinY, block.blockBoundsMinZ,
            block.blockBoundsMaxX, block.blockBoundsMaxY, block.blockBoundsMaxZ,
        )
        info.add("bounds", JsonArray().also { a -> bounds.forEach { a.addNumber(it) } })
        blockShapes[meta] = shapeOf(block)
        blockBounds[meta] = bounds

        // connected-texture tiles (Angelica/MCPatcher `mcpatcher/ctm/...`) depend on neighbours: use the base icon
        val worldIcons = (0 until 6).map { side ->
            val icon = safe { block.getIcon(world, FakeWorld.X, FakeWorld.Y, FakeWorld.Z, side) }
                ?.takeUnless { TextureExporter.isErrorIcon(it.iconName) }
            if (icon == null || (TextureExporter.isCtmTile(icon.iconName) && itemIcons[side] != null)) itemIcons[side] else icon
        }
        info.add("icons", JsonArray().also { a -> worldIcons.forEach { a.addString(it?.iconName) } })
        safe { block.getRenderColor(meta) }?.let { info.addProperty("renderColor", String.format("#%06x", it and 0xFFFFFF)) }

        val tint = tintOf(block)
        info.addProperty(
            "tint",
            when (tint) {
                Tint.None -> "none"
                is Tint.Fixed -> String.format("#%06x", tint.rgb)
                is Tint.Biome -> tint.calculator
                is Tint.Unmatched -> String.format("biome-unmatched #%06x", tint.captureRgb)
            },
        )

        val fullCube = bounds.contentEquals(FULL_BOUNDS)
        val result = when {
            renderType == 0 && fullCube -> cube(block, worldIcons, tint, namespace, value, models, FULL_BOUNDS, bbox = false)
            renderType == -1 -> {
                if (probed && meta != 0) return StateResult.Invalid
                val tesr = tileEntity != null && TileEntityRendererDispatcher.instance.hasSpecialRenderer(tileEntity)
                StateResult.Failed(if (tesr) "tesr_only" else "invisible")
            }
            else -> quads(block, tileEntity, tint, namespace, value, models, probed && meta != 0)
        }
        if (result !is StateResult.Failed || result.reason !in BBOX_FALLBACK_REASONS) return result

        // Static fallback for geometry we cannot capture (tile-entity special renderers, renderers that draw nothing
        // in an empty world): the block's bounding box with its own icons.
        val icons = worldIcons.map { it ?: worldIcons.firstOrNull { i -> i != null } ?: itemIcons.firstOrNull { i -> i != null } }
        if (icons.any { it == null }) return result
        val box = if (bounds[3] > bounds[0] && bounds[4] > bounds[1] && bounds[5] > bounds[2]) bounds else FULL_BOUNDS
        val fallback = cube(block, icons, tint, namespace, value, models, box, bbox = true)
        if (fallback !is StateResult.Model) return result
        info.addProperty("fallbackFor", result.reason)
        return fallback
    }

    private fun cube(
        block: Block,
        icons: List<IIcon?>,
        tint: Tint,
        namespace: String,
        value: String,
        models: HashMap<String, String>,
        bounds: DoubleArray,
        bbox: Boolean,
    ): StateResult {
        val textureKeys = icons.map { textures.export(it) }
        if (textureKeys.any { it == null }) {
            val missing = icons.zip(textureKeys).filter { it.second == null }.map { it.first?.iconName }
            return StateResult.Failed("no_texture: $missing")
        }
        val tinted = tint !is Tint.None
        val light = block.lightValue
        val from = DoubleArray(3) { round16(bounds[it]) }
        val to = DoubleArray(3) { round16(bounds[it + 3]) }
        val signature = "cube|" + textureKeys.joinToString("|") + "|" + tinted + "|" + light + "|" +
            from.joinToString(",") + "|" + to.joinToString(",")
        val model = models.getOrPut(signature) {
            val key = "$namespace:block/$value/${if (bbox) "bbox_" else ""}${models.size}"
            val texturesJson = JsonObject()
            val faces = JsonObject()
            for (side in 0 until 6) {
                texturesJson.addProperty(SIDES[side], textureKeys[side])
                // a face lies on the block boundary: down/north/west at 0, up/south/east at 16
                val axis = SIDE_AXIS[side]
                val onBoundary = if (side % 2 == 0) from[axis] == 0.0 else to[axis] == 16.0
                faces.add(
                    SIDES[side],
                    JsonObject().apply {
                        addProperty("texture", "#" + SIDES[side])
                        if (onBoundary) addProperty("cullface", SIDES[side])
                        if (tinted) addProperty("tintindex", 0)
                    },
                )
            }
            texturesJson.addProperty("particle", textureKeys[1])
            val element = JsonObject().apply {
                add("from", JsonArray().also { a -> from.forEach { a.addNumber(it) } })
                add("to", JsonArray().also { a -> to.forEach { a.addNumber(it) } })
                if (light > 0) addProperty("light_emission", light)
                add("faces", faces)
            }
            val json = JsonObject().apply {
                add("textures", texturesJson)
                add("elements", JsonArray().also { it.add(element) })
            }
            pack.json(modelPath(key), json)
            modelTextures[key] = textureKeys.filterNotNull()
            key
        }
        return StateResult.Model(model, quads = false, color = colorConfig(tint), bbox = bbox)
    }

    private fun colorConfig(tint: Tint): String? = when (tint) {
        Tint.None -> null
        is Tint.Fixed -> String.format("#%06x", tint.rgb)
        is Tint.Biome -> tint.calculator
        is Tint.Unmatched -> String.format("#%06x", tint.captureRgb)
    }

    private fun quads(
        block: Block,
        tileEntity: TileEntity?,
        tint: Tint,
        namespace: String,
        value: String,
        models: HashMap<String, String>,
        probedMeta: Boolean,
    ): StateResult {
        val captureTint = when (tint) {
            is Tint.Biome -> tint.captureRgb
            else -> -1
        }
        val captured = capture(block)
        if (captured.isEmpty()) {
            if (probedMeta) return StateResult.Invalid
            val tesr = tileEntity != null && TileEntityRendererDispatcher.instance.hasSpecialRenderer(tileEntity)
            return StateResult.Failed(if (tesr) "tesr_only" else "no_geometry")
        }

        val textureIndex = LinkedHashMap<String, Int>()
        val quadsJson = JsonArray()
        var anyTint = false
        val light = block.lightValue
        var errorQuads = 0
        for (quad in captured) {
            val sprite = textures.spriteAt(quad.uc, quad.vc)
            if (sprite == null) {
                unmappedQuads++
                continue
            }
            val texture = if (TextureExporter.isCtmTile(sprite.iconName)) null else textures.export(sprite)
            if (texture == null) {
                errorQuads++
                continue
            }
            val t = textureIndex.getOrPut(texture) { textureIndex.size }
            val du = sprite.maxU - sprite.minU
            val dv = sprite.maxV - sprite.minV

            val vertices = JsonArray()
            for (i in 0 until 4) {
                vertices.add(
                    JsonArray().apply {
                        addNumber(round((quad.x[i] - FakeWorld.X) * 16))
                        addNumber(round((quad.y[i] - FakeWorld.Y) * 16))
                        addNumber(round((quad.z[i] - FakeWorld.Z) * 16))
                        addNumber(round((quad.u[i] - sprite.minU) / du * 16))
                        addNumber(round((quad.v[i] - sprite.minV) / dv * 16))
                    },
                )
            }

            // remove vanilla's directional face shade (non-AO path) from the vertex color
            val shade = faceShade(quad)
            var r = (quad.r / shade).coerceAtMost(1f)
            var g = (quad.g / shade).coerceAtMost(1f)
            var b = (quad.b / shade).coerceAtMost(1f)
            var tinted = false
            if (captureTint >= 0 && near(r, g, b, captureTint)) {
                tinted = true
                anyTint = true
                r = 1f
                g = 1f
                b = 1f
            }

            quadsJson.add(
                JsonObject().apply {
                    addProperty("texture", "#t$t")
                    add("vertices", vertices)
                    val rgb = (r * 255).roundToInt() shl 16 or ((g * 255).roundToInt() shl 8) or (b * 255).roundToInt()
                    if (rgb != 0xFFFFFF) addProperty("color", String.format("#%06x", rgb))
                    if (tinted) addProperty("tint", true)
                    cullface(quad)?.let { addProperty("cullface", it) }
                    if (light > 0) addProperty("light_emission", light)
                },
            )
        }
        // a capture with placeholder sprites is broken as a whole (partially drawn CTM, missing overlays)
        if (quadsJson.size() == 0 || errorQuads > 0) {
            if (probedMeta) return StateResult.Invalid
            return StateResult.Failed(if (errorQuads > 0) "error_icon" else "unmapped_uv")
        }

        val texturesJson = JsonObject()
        for ((key, index) in textureIndex) texturesJson.addProperty("t$index", key)
        val json = JsonObject().apply {
            add("textures", texturesJson)
            add("gtnh_quads", quadsJson)
        }
        val signature = PackWriter.GSON.toJson(json)
        val model = models.getOrPut(signature) {
            val key = "$namespace:gtnh_quads/$value/${models.size}"
            pack.json(modelPath(key), json)
            modelTextures[key] = textureIndex.keys.toList()
            key
        }
        val color = if (anyTint) (tint as Tint.Biome).calculator else null
        return StateResult.Model(model, quads = true, color = color)
    }

    private fun modelPath(key: String): String {
        val namespace = key.substringBefore(':')
        return "assets/$namespace/models/${key.substringAfter(':')}.json"
    }

    /** A captured quad, copied out of GTNHLib's pooled quads. */
    private class Quad(
        val x: FloatArray,
        val y: FloatArray,
        val z: FloatArray,
        val u: FloatArray,
        val v: FloatArray,
        val r: Float,
        val g: Float,
        val b: Float,
    ) {
        val uc = u.average().toFloat()
        val vc = v.average().toFloat()
    }

    private fun capture(block: Block): List<Quad> {
        val result = ArrayList<Quad>()
        renderBlocks.blockAccess = world
        renderBlocks.renderAllFaces = true
        for (pass in 0..1) {
            if (safe { block.canRenderInPass(pass) } != true) continue
            setWorldRenderPass(pass)
            val tessellator = TessellatorManager.startCapturingAndGet()
            var error: Throwable? = null
            try {
                tessellator.startDrawingQuads()
                renderBlocks.renderBlockByRenderType(block, FakeWorld.X, FakeWorld.Y, FakeWorld.Z)
            } catch (t: Throwable) {
                error = t
            }
            try {
                val quads = TessellatorManager.stopCapturingToPooledQuads()
                for (q in quads) {
                    val x = FloatArray(4) { q.getX(it) }
                    val y = FloatArray(4) { q.getY(it) }
                    val z = FloatArray(4) { q.getZ(it) }
                    val u = FloatArray(4) { q.getTexU(it) }
                    val v = FloatArray(4) { q.getTexV(it) }
                    var r = 0f
                    var g = 0f
                    var b = 0f
                    for (i in 0 until 4) {
                        val c = q.getColor(i) // ABGR (little endian tessellator layout)
                        r += (c and 0xFF) / 255f
                        g += (c shr 8 and 0xFF) / 255f
                        b += (c shr 16 and 0xFF) / 255f
                    }
                    result.add(Quad(x, y, z, u, v, r / 4, g / 4, b / 4))
                }
            } finally {
                tessellator.clearQuads()
            }
            if (error != null) throw error
        }
        renderBlocks.renderAllFaces = false
        return result
    }

    private fun tintOf(block: Block): Tint {
        val colors = IntArray(PROBE_BIOMES.size)
        try {
            for ((i, biome) in PROBE_BIOMES.withIndex()) {
                world.biome = biome
                colors[i] = block.colorMultiplier(world, FakeWorld.X, FakeWorld.Y, FakeWorld.Z) and 0xFFFFFF
            }
        } catch (_: Throwable) {
            world.biome = BiomeGenBase.plains
            return Tint.None
        }
        world.biome = BiomeGenBase.plains
        if (colors.all { it == colors[0] }) return if (colors[0] == 0xFFFFFF) Tint.None else Tint.Fixed(colors[0])
        val x = FakeWorld.X
        val y = FakeWorld.Y
        val z = FakeWorld.Z
        val calculators = listOf<Pair<String, (BiomeGenBase) -> Int>>(
            "@grass" to { it.getBiomeGrassColor(x, y, z) },
            "@foliage" to { it.getBiomeFoliageColor(x, y, z) },
            "@water" to { it.waterColorMultiplier },
        )
        for ((calculator, fn) in calculators) {
            if (PROBE_BIOMES.withIndex().all { (i, biome) -> nearRgb(colors[i], safe { fn(biome) and 0xFFFFFF } ?: -1) }) {
                return Tint.Biome(calculator, colors[0])
            }
        }
        return Tint.Unmatched(colors[0])
    }

    companion object {
        private val SIDES = arrayOf("down", "up", "north", "south", "west", "east")

        /** Blocks drawn by a dedicated BlueMap renderer from `gtnh-render` (GregTech, Phase 4). */
        private val RENDERER_OVERRIDES: Map<String, String> = buildMap {
            put("gregtech:gt.blockmachines", "gtnh:gt_machine")
            put("gregtech:gt.blockframes", "gtnh:gt_frame")
            for (i in 2..7) put("gregtech:gt.blockores$i", "gtnh:gt_ore")
        }
        private val SIDE_AXIS = intArrayOf(1, 1, 2, 2, 0, 0)
        private val FULL_BOUNDS = doubleArrayOf(0.0, 0.0, 0.0, 1.0, 1.0, 1.0)
        // error_icon: the renderer drew placeholder sprites in the fake world (e.g. Chisel's CTM renderer); the
        // block's own icons on its bounds are the base-tile fallback the plan prescribes for CTM
        private val BBOX_FALLBACK_REASONS = setOf("tesr_only", "invisible", "no_geometry", "error_icon")
        private val SHADE = floatArrayOf(0.5f, 1f, 0.8f, 0.8f, 0.6f, 0.6f)
        private const val MAX_LISTED_FAILURES = 200

        /** plains first: it is the capture biome. */
        private val PROBE_BIOMES = listOf(
            BiomeGenBase.plains, BiomeGenBase.desert, BiomeGenBase.swampland, BiomeGenBase.jungle,
            BiomeGenBase.taiga, BiomeGenBase.icePlains, BiomeGenBase.roofedForest,
        )

        private val worldRenderPassField = try {
            ForgeHooksClient::class.java.getDeclaredField("worldRenderPass").also { it.isAccessible = true }
        } catch (_: Exception) {
            null
        }

        fun setWorldRenderPass(pass: Int) {
            worldRenderPassField?.setInt(null, pass)
        }

        private inline fun <T> safe(block: () -> T): T? = try {
            block()
        } catch (_: Throwable) {
            null
        }

        fun reason(t: Throwable): String {
            val message = t.message?.lineSequence()?.firstOrNull()?.take(120)
            return t.javaClass.simpleName + (message?.let { " $it" } ?: "")
        }

        private fun round(f: Float): Float = (f * 1000).roundToInt() / 1000f

        private fun round16(d: Double): Double = (d * 16 * 1000).roundToInt() / 1000.0

        private fun near(r: Float, g: Float, b: Float, rgb: Int): Boolean =
            abs(r * 255 - (rgb shr 16 and 0xFF)) <= 3 &&
                abs(g * 255 - (rgb shr 8 and 0xFF)) <= 3 &&
                abs(b * 255 - (rgb and 0xFF)) <= 3

        private fun nearRgb(a: Int, b: Int): Boolean =
            b >= 0 &&
                abs((a shr 16 and 0xFF) - (b shr 16 and 0xFF)) <= 2 &&
                abs((a shr 8 and 0xFF) - (b shr 8 and 0xFF)) <= 2 &&
                abs((a and 0xFF) - (b and 0xFF)) <= 2

        /** Unit normal (counter-clockwise front, as the tessellator draws), or null for degenerate quads. */
        private fun normal(q: Quad): FloatArray? {
            val ax = q.x[2] - q.x[0]
            val ay = q.y[2] - q.y[0]
            val az = q.z[2] - q.z[0]
            val bx = q.x[3] - q.x[1]
            val by = q.y[3] - q.y[1]
            val bz = q.z[3] - q.z[1]
            val nx = ay * bz - az * by
            val ny = az * bx - ax * bz
            val nz = ax * by - ay * bx
            val len = sqrt(nx * nx + ny * ny + nz * nz)
            if (len < 1e-9f) return null
            return floatArrayOf(nx / len, ny / len, nz / len)
        }

        /** Vanilla non-AO shade of an axis-aligned face (1 for diagonal quads, which vanilla does not shade). */
        private fun faceShade(q: Quad): Float {
            val n = normal(q) ?: return 1f
            return when {
                abs(n[1]) > 0.99f -> if (n[1] > 0) SHADE[1] else SHADE[0]
                abs(n[2]) > 0.99f -> SHADE[2]
                abs(n[0]) > 0.99f -> SHADE[4]
                else -> 1f
            }
        }

        /** Block face a quad lies on while facing outwards, if any. */
        private fun cullface(q: Quad): String? {
            fun all(values: FloatArray, target: Float) = values.all { abs(it - target) < 1e-4f }
            val n = normal(q) ?: return null
            val x0 = FakeWorld.X.toFloat()
            val y0 = FakeWorld.Y.toFloat()
            val z0 = FakeWorld.Z.toFloat()
            return when {
                n[1] < -0.99f && all(q.y, y0) -> "down"
                n[1] > 0.99f && all(q.y, y0 + 1) -> "up"
                n[2] < -0.99f && all(q.z, z0) -> "north"
                n[2] > 0.99f && all(q.z, z0 + 1) -> "south"
                n[0] < -0.99f && all(q.x, x0) -> "west"
                n[0] > 0.99f && all(q.x, x0 + 1) -> "east"
                else -> null
            }
        }

        fun parseExtraStates(file: File?): Map<String, Set<Int>> {
            if (file == null) return emptyMap()
            val result = HashMap<String, MutableSet<Int>>()
            file.forEachLine { line ->
                val trimmed = line.substringBefore('#').trim()
                if (trimmed.isEmpty()) return@forEachLine
                // the meta is the last token: registry names may contain spaces
                val split = trimmed.lastIndexOfAny(charArrayOf(' ', '\t'))
                require(split > 0) { "extra states line must be '<modid:name> <meta>': $line" }
                val meta = trimmed.substring(split + 1).toInt()
                result.getOrPut(trimmed.substring(0, split).trim()) { HashSet() }.add(meta)
            }
            return result
        }
    }
}
