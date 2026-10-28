package de.bluecolored.bluemap.gtnh.exporter

import com.google.gson.JsonObject
import cpw.mods.fml.common.Loader
import net.minecraft.block.Block
import net.minecraft.client.Minecraft
import net.minecraft.util.IIcon
import net.minecraft.util.ResourceLocation
import java.io.File
import java.util.TreeMap
import java.util.zip.ZipFile

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

    /** Copies the raw [ASSETS] from the mod jars (resource-pack overrides win) and writes the index. */
    fun copyAssets(): Int {
        val index = TreeMap<String, String>()
        val sources = Loader.instance().activeModList.mapNotNull { it.source }.distinct()
        for (source in sources) {
            for ((domain, path) in listAssets(source)) {
                val spec = ASSETS.firstOrNull { it.domain == domain && path.startsWith(it.prefix) && it.extensions.any(path::endsWith) }
                    ?: continue
                val key = "$domain:$path"
                if (key in index) continue
                val bytes = try {
                    mc.resourceManager.getResource(ResourceLocation(domain, path)).inputStream.use { it.readBytes() }
                } catch (_: Exception) {
                    continue
                }
                val target = zipPath(domain, path)
                if (path.endsWith(".png.mcmeta")) {
                    TextureExporter.sanitizeMcmeta(bytes)?.let { pack.bytes(target, it) } ?: continue
                } else {
                    pack.bytes(target, bytes)
                }
                index[key] = target
            }
        }
        pack.json(
            "gtnh/mods/assets.json",
            JsonObject().apply {
                addProperty("format", 1)
                add("assets", JsonObject().also { o -> index.forEach { (k, v) -> o.addProperty(k, v) } })
            },
        )
        return index.size
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

        val ASSETS = listOf(
            AssetSpec("chisel", "textures/blocks/", PNG),
            AssetSpec("ironchest", "textures/model/", PNG),
            AssetSpec("bibliocraft", "models/", listOf(".obj")),
            AssetSpec("bibliocraft", "textures/models/", PNG),
            AssetSpec("appliedenergistics2", "textures/blocks/", PNG),
            AssetSpec("enderio", "textures/blocks/", PNG),
        )

        /** Same normalization as [TextureExporter]: N(domain), N(segments), `textures/blocks/` -> `textures/block/`. */
        fun zipPath(domain: String, path: String): String {
            val renamed = if (path.startsWith("textures/blocks/")) "textures/block/" + path.removePrefix("textures/blocks/") else path
            val namespace = if (domain == "minecraft") PackKeys.LEGACY_MINECRAFT_NAMESPACE else de.bluecolored.bluemap.gtnh.legacy.ResourceKeys.normalize(domain)
            return "assets/$namespace/" + PackKeys.normalizePath(renamed)
        }

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
