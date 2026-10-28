package de.bluecolored.bluemap.gtnh.exporter

import com.google.gson.JsonObject
import cpw.mods.fml.common.Loader
import net.minecraft.block.Block
import net.minecraft.client.Minecraft
import net.minecraft.util.IIcon
import net.minecraft.util.ResourceLocation
import java.awt.Image
import java.awt.image.BufferedImage
import java.io.ByteArrayInputStream
import java.io.File
import java.util.TreeMap
import java.util.zip.ZipFile
import javax.imageio.ImageIO

/**
 * Data for the mod-specific BlueMap renderers in `gtnh-render` (`render/mods`): raw mod assets (`gtnh/mods/assets.json`),
 * Chisel connected-texture variations (`gtnh/mods/chisel.json`) and the renderer keys of their blockstate variants.
 */
class ModData(private val mc: Minecraft, private val pack: PackWriter, private val textures: TextureExporter) {
    private val chiselBlocks = JsonObject()
    var chiselStates = 0
        private set

    /** Chisel variation per meta of [block] (`meta -> {"type", "fullCube", "textures"}`), empty if not a CTM block. */
    fun chisel(block: Block, metas: Collection<Int>, fullCube: (Int) -> Boolean): Map<Int, JsonObject> {
        val ctmBlock = ICTM_BLOCK ?: return emptyMap()
        if (!ctmBlock.isInstance(block)) return emptyMap()
        val getManager = ctmBlock.getMethod("getManager", Int::class.javaPrimitiveType)
        val result = TreeMap<Int, JsonObject>()
        for (meta in metas) {
            val manager = try {
                getManager.invoke(block, meta)
            } catch (_: Exception) {
                null
            } ?: continue
            val (type, icons) = describe(manager) ?: continue
            val texturesJson = JsonObject()
            for ((key, icon) in icons) texturesJson.addProperty(key, textures.export(icon) ?: continue)
            if (texturesJson.entrySet().size != icons.size) continue
            result[meta] = JsonObject().apply {
                addProperty("type", type)
                addProperty("fullCube", fullCube(meta))
                add("textures", texturesJson)
            }
        }
        return result
    }

    fun addChisel(key: String, variations: Map<Int, JsonObject>) {
        if (variations.isEmpty()) return
        chiselBlocks.add(key, JsonObject().also { o -> variations.forEach { (meta, v) -> o.add(meta.toString(), v) } })
        chiselStates += variations.size
    }

    private fun describe(manager: Any): Pair<String, Map<String, IIcon?>>? {
        val cls = manager.javaClass
        if (cls.name == "team.chisel.ctmlib.SubmapManagerCTM") {
            return "CTMX" to mapOf("base" to baseIcon(call(manager, "getSubmapSmall")), "ctm" to baseIcon(call(manager, "getSubmap")))
        }
        val variationInfo = IVARIATION_INFO ?: return null
        if (!variationInfo.isInstance(manager)) return null
        val type = (call(manager, "getType") as? Enum<*>)?.name ?: return null
        val inner = field(manager, "manager") ?: return null
        if (inner.javaClass.name == "team.chisel.ctmlib.SubmapManagerCTM") {
            return "CTMX" to mapOf("base" to baseIcon(call(inner, "getSubmapSmall")), "ctm" to baseIcon(call(inner, "getSubmap")))
        }
        val cached = runCatching { call(inner, "getCachedObject") }.getOrNull() ?: return null
        return when (type) {
            "CTMX" -> type to mapOf("base" to call(cached, "getLeft") as? IIcon, "ctm" to baseIcon(call(cached, "getMiddle")))
            "V9", "V4", "R16", "R9", "R4" -> type to mapOf("sheet" to baseIcon(cached))
            "CTMV", "CTMH" -> type to mapOf("sheet" to baseIcon(call(cached, "getLeft")), "top" to call(cached, "getRight") as? IIcon)
            else -> null
        }
    }

    private fun baseIcon(submap: Any?): IIcon? = submap?.let { call(it, "getBaseIcon") as? IIcon }

    private fun call(target: Any, method: String): Any? {
        var cls: Class<*>? = target.javaClass
        while (cls != null) {
            cls.declaredMethods.firstOrNull { it.name == method && it.parameterCount == 0 }?.let {
                it.isAccessible = true
                return it.invoke(target)
            }
            cls = cls.superclass
        }
        return target.javaClass.getMethod(method).invoke(target)
    }

    private fun field(target: Any, name: String): Any? {
        var cls: Class<*>? = target.javaClass
        while (cls != null) {
            cls.declaredFields.firstOrNull { it.name == name }?.let {
                it.isAccessible = true
                return it.get(target)
            }
            cls = cls.superclass
        }
        return null
    }

    /**
     * Copies the raw [ASSETS] from the mod jars (resource-pack overrides win), adds the generated [WHITE] texture and
     * writes the index. Textures are re-encoded as PNG when they are not (some mods ship JPEGs named `.png`) and
     * shrunk to [MAX_TEXTURE_WIDTH] (TESR model textures up to 2048 px would bloat BlueMap's texture file).
     */
    fun copyAssets(): Int {
        val index = TreeMap<String, String>()
        fun copy(domain: String, path: String) {
            val key = "$domain:$path"
            if (key in index) return
            val bytes = try {
                mc.resourceManager.getResource(ResourceLocation(domain, path)).inputStream.use { it.readBytes() }
            } catch (_: Exception) {
                return
            }
            val target = zipPath(domain, path)
            val written = when {
                path.endsWith(".png.mcmeta") -> TextureExporter.sanitizeMcmeta(bytes)?.let { pack.bytes(target, it) } != null
                path.endsWith(".png") -> writeTexture(target, bytes)
                else -> pack.bytes(target, bytes).let { true }
            }
            if (written) index[key] = target
        }
        val sources = Loader.instance().activeModList.mapNotNull { it.source }.distinct()
        for (source in sources) {
            for ((domain, path) in listAssets(source)) {
                if (ASSETS.any { it.domain == domain && path.startsWith(it.prefix) && it.extensions.any(path::endsWith) }) copy(domain, path)
            }
        }
        // single files (vanilla's are not in a mod source)
        for (spec in ASSETS) {
            if (spec.prefix.endsWith("/")) continue
            copy(spec.domain, spec.prefix)
            if (spec.prefix.endsWith(".png") && ".png.mcmeta" in spec.extensions) copy(spec.domain, spec.prefix + ".mcmeta")
        }
        val white = BufferedImage(16, 16, BufferedImage.TYPE_INT_ARGB)
        for (x in 0 until 16) for (y in 0 until 16) white.setRGB(x, y, -1)
        pack.png(WHITE_PATH, white)
        index[WHITE] = WHITE_PATH
        pack.json(
            "gtnh/mods/assets.json",
            JsonObject().apply {
                addProperty("format", 1)
                add("assets", JsonObject().also { o -> index.forEach { (k, v) -> o.addProperty(k, v) } })
            },
        )
        return index.size
    }

    private fun writeTexture(target: String, bytes: ByteArray): Boolean {
        val png = bytes.size > 4 && bytes[0] == 0x89.toByte() && bytes[1] == 'P'.code.toByte() && bytes[2] == 'N'.code.toByte()
        val image = try {
            ImageIO.read(ByteArrayInputStream(bytes))
        } catch (_: Exception) {
            null
        } ?: return png && pack.bytes(target, bytes).let { true }
        if (png && image.width <= MAX_TEXTURE_WIDTH) {
            pack.bytes(target, bytes)
            return true
        }
        val scale = minOf(1.0, MAX_TEXTURE_WIDTH.toDouble() / image.width)
        val width = maxOf(1, (image.width * scale).toInt())
        val height = maxOf(1, (image.height * scale).toInt())
        val out = BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB)
        val g = out.createGraphics()
        g.drawImage(image.getScaledInstance(width, height, Image.SCALE_AREA_AVERAGING), 0, 0, null)
        g.dispose()
        pack.png(target, out)
        return true
    }

    /**
     * Thaumcraft's aspect colours (`gtnh/mods/aspects.json`: tag -> `#rrggbb`) of every registered aspect, addons'
     * included, for essentia drawn by TESRs. Nothing when Thaumcraft is absent.
     */
    fun writeAspects(): Int {
        val aspects = runCatching {
            val type = Class.forName("thaumcraft.api.aspects.Aspect")
            val map = type.getField("aspects").get(null) as Map<*, *>
            val color = type.getMethod("getColor")
            map.mapNotNull { (tag, aspect) -> (tag as? String)?.let { it to (color.invoke(aspect) as Int) } }
        }.getOrDefault(emptyList())
        if (aspects.isEmpty()) return 0
        pack.json(
            "gtnh/mods/aspects.json",
            JsonObject().apply {
                addProperty("format", 1)
                add("aspects", JsonObject().also { o -> aspects.sortedBy { it.first }.forEach { (tag, c) -> o.addProperty(tag, String.format("#%06x", c and 0xFFFFFF)) } })
            },
        )
        return aspects.size
    }

    fun writeChisel() {
        pack.json(
            "gtnh/mods/chisel.json",
            JsonObject().apply {
                addProperty("format", 1)
                add("blocks", chiselBlocks)
            },
        )
    }

    private fun listAssets(source: File): List<Pair<String, String>> {
        val result = ArrayList<Pair<String, String>>()
        fun add(entry: String) {
            if (!entry.startsWith("assets/")) return
            val rest = entry.removePrefix("assets/")
            val slash = rest.indexOf('/')
            if (slash <= 0 || rest.endsWith("/")) return
            result.add(rest.substring(0, slash) to rest.substring(slash + 1))
        }
        try {
            if (source.isDirectory) {
                source.walkTopDown().filter { it.isFile }.forEach { add(it.relativeTo(source).invariantSeparatorsPath) }
            } else if (source.isFile && (source.name.endsWith(".jar") || source.name.endsWith(".zip"))) {
                ZipFile(source).use { zip -> zip.entries().asSequence().forEach { add(it.name) } }
            }
        } catch (_: Exception) {
        }
        return result
    }

    data class AssetSpec(val domain: String, val prefix: String, val extensions: List<String>)

    companion object {
        private val PNG = listOf(".png", ".png.mcmeta")

        /** Models and textures that only the community addons' TESRs bind (gtnh-render `render/mods/addons`). */
        private val TESR_ASSETS = listOf(
            // Twist Space Technology
            AssetSpec("gtnhcommunitymod", "model/", PNG + ".obj"),
            AssetSpec("thaumcraft", "textures/misc/particlefield32.png", PNG),
            // GT: Not Leisure
            AssetSpec("sciencenotleisure", "models/block/beam_former.json", listOf(".json")),
            AssetSpec("sciencenotleisure", "textures/blocks/beam_former/", PNG),
            AssetSpec("sciencenotleisure", "models/block/pigmee_fumo.json", listOf(".json")),
            AssetSpec("sciencenotleisure", "textures/blocks/pigmee_fumo.png", PNG),
            AssetSpec("sciencenotleisure", "models/player_doll.obj", listOf(".obj")),
            AssetSpec("sciencenotleisure", "models/skin.png", PNG),
            AssetSpec("sciencenotleisure", "textures/blocks/water_candle.png", PNG),
            AssetSpec("sciencenotleisure", "textures/blocks/ender_elevator.png", PNG),
            AssetSpec("sciencenotleisure", "textures/blocks/ender_elevator_overlay.png", PNG),
            AssetSpec("minecraft", "textures/entity/beacon_beam.png", PNG),
            AssetSpec("thaumcraft", "textures/blocks/animatedglow.png", PNG),
            // Box++
            AssetSpec("boxplusplus", "models/Ring.obj", listOf(".obj")),
            AssetSpec("boxplusplus", "textures/models/ring.png", PNG),
            // 123Technology (its rocket is GalaxySpace's tier 8 rocket)
            AssetSpec("123technology", "models/", PNG + ".obj"),
            AssetSpec("galaxyspace", "models/tier8rocket.obj", listOf(".obj")),
            AssetSpec("galaxyspace", "textures/model/tier8rocket.png", PNG),
        )

        val ASSETS = listOf(
            AssetSpec("chisel", "textures/blocks/", PNG),
            AssetSpec("ironchest", "textures/model/", PNG),
            AssetSpec("bibliocraft", "models/", listOf(".obj")),
            AssetSpec("bibliocraft", "textures/models/", PNG),
            AssetSpec("appliedenergistics2", "textures/blocks/", PNG),
            AssetSpec("enderio", "textures/blocks/", PNG),
        ) + TESR_ASSETS

        /** Generated plain white texture for untextured TESR geometry (gtnh-render `AddonTesrs.WHITE`). */
        const val WHITE = "gtnh:textures/misc/white.png"
        private const val WHITE_PATH = "assets/gtnh/textures/misc/white.png"

        /** Raw textures wider than this are scaled down. */
        const val MAX_TEXTURE_WIDTH = 512

        /**
         * Same normalization as [TextureExporter]: N(domain), N(segments), `textures/blocks/` -> `textures/block/`.
         * Textures outside `textures/` (TESR model textures next to their models) move under it, where BlueMap
         * finds textures.
         */
        fun zipPath(domain: String, path: String): String {
            val renamed = when {
                path.startsWith("textures/blocks/") -> "textures/block/" + path.removePrefix("textures/blocks/")
                !path.startsWith("textures/") && (path.endsWith(".png") || path.endsWith(".png.mcmeta")) -> "textures/$path"
                else -> path
            }
            val namespace = if (domain == "minecraft") PackKeys.LEGACY_MINECRAFT_NAMESPACE else de.bluecolored.bluemap.gtnh.legacy.ResourceKeys.normalize(domain)
            return "assets/$namespace/" + PackKeys.normalizePath(renamed)
        }

        /** Tile entity classes whose TESR has a static port in gtnh-render (`render/mods/addons`). */
        private val ADDON_TESR_TILE_ENTITIES = setOf(
            // Twist Space Technology
            "com.Nxer.TwistSpaceTechnology.common.tile.TileStar",
            "com.Nxer.TwistSpaceTechnology.common.tile.TileEyeOfWoodRender",
            "com.Nxer.TwistSpaceTechnology.common.tile.TilePowerChair",
            "com.Nxer.TwistSpaceTechnology.common.tile.TileArcaneHole",
            "com.Nxer.TwistSpaceTechnology.common.entity.TileEntityLaserBeacon",
            // GT: Not Leisure
            "com.science.gtnl.common.block.blocks.tile.TileEntityBeamFormer",
            "com.science.gtnl.common.block.blocks.tile.TileEntityLaserBeacon",
            "com.science.gtnl.common.block.blocks.tile.TileEntityMultiEssentiaJar",
            "com.science.gtnl.common.block.blocks.tile.TileEntityWaterCandle",
            "com.science.gtnl.common.block.blocks.tile.TileEntityPlayerDoll",
            "com.science.gtnl.common.block.blocks.tile.TileEntityPigmeeFumo",
            "com.science.gtnl.common.block.blocks.tile.TileEntityEnderElevator",
            // Box++
            "com.silvermoon.boxplusplus.common.tileentities.TEBoxRing",
            // 123Technology
            "com.newmaa.othtech.common.blocks.oterender.OTERocketRender",
            "com.newmaa.othtech.common.blocks.OTEModelAsuka",
            "com.newmaa.othtech.common.blocks.OTEModelAyanami",
            "com.newmaa.othtech.common.blocks.OTEModelTallHat",
        )

        /** `gtnh:addon_tesr` for a block meta whose tile entity (class name) has a TESR port. */
        fun rendererForTileEntity(className: String): String? = if (className in ADDON_TESR_TILE_ENTITIES) "gtnh:addon_tesr" else null

        private val ICTM_BLOCK: Class<*>? = runCatching { Class.forName("team.chisel.ctmlib.ICTMBlock") }.getOrNull()
        private val IVARIATION_INFO: Class<*>? = runCatching { Class.forName("com.cricketcraft.chisel.api.carving.IVariationInfo") }.getOrNull()

        /** Chisel's CTM ISBRH render id (`ClientUtils.renderCTMId`, assigned at client init), or `Int.MIN_VALUE`. */
        fun chiselCtmRenderId(): Int = runCatching {
            Class.forName("com.cricketcraft.chisel.api.rendering.ClientUtils").getField("renderCTMId").getInt(null)
        }.getOrDefault(Int.MIN_VALUE)

        /** Renderer keys of the mod renderers; chisel is per meta (CTM variation on a full cube). */
        fun rendererFor(name: String): String? = when {
            name == "IronChest:BlockIronChest" -> "gtnh:iron_chest"
            name.startsWith("BiblioCraft:") -> "gtnh:bibliocraft"
            name == "appliedenergistics2:tile.BlockCableBus" -> "gtnh:ae2_cable_bus"
            name == "EnderIO:blockConduitBundle" -> "gtnh:enderio_conduit"
            else -> null
        }
    }
}
