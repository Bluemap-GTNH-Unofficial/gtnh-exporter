package de.bluecolored.bluemap.gtnh.exporter.gt

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import de.bluecolored.bluemap.gtnh.exporter.BlockExporter
import de.bluecolored.bluemap.gtnh.exporter.PackWriter
import de.bluecolored.bluemap.gtnh.exporter.TextureExporter
import de.bluecolored.bluemap.gtnh.exporter.addNumber
import gregtech.api.enums.Textures
import gregtech.api.interfaces.IColorModulationContainer
import gregtech.api.interfaces.IIconContainer
import gregtech.api.interfaces.ITexture
import gregtech.api.render.ISBRContext
import gregtech.common.render.GTCopiedBlockTextureRender
import gregtech.common.render.GTMultiTextureRender
import gregtech.common.render.GTRenderedTexture
import gregtech.common.render.GTSidedTextureRender
import gregtech.common.render.IIconTexture
import net.minecraft.block.Block
import net.minecraft.util.IIcon
import java.lang.reflect.Field
import java.util.IdentityHashMap
import java.util.TreeMap

/**
 * Flattens GregTech [ITexture]s into per-face layer lists and interns the layer descriptors into [table]
 * (one table per output file; a face is a JSON array of indices into it, bottom layer first).
 */
internal class Layers(private val textures: TextureExporter) {
    val table = JsonArray()
    private val index = HashMap<String, Int>()
    private val cache = IdentityHashMap<ITexture, Array<IntArray?>>()

    /** Implementations without a dedicated descriptor, by class name. */
    val unknownClasses = TreeMap<String, Int>()

    fun intern(layer: JsonObject): Int =
        index.getOrPut(PackWriter.GSON.toJson(layer)) {
            table.add(layer)
            table.size() - 1
        }

    /** The layers of [layers] (GT's `ITexture[]` for one face) as rendered on face [side] (ForgeDirection ordinal). */
    fun face(layers: Array<ITexture?>?, side: Int): JsonArray {
        val out = JsonArray()
        layers?.forEach { tex -> layerIds(tex, side).forEach { out.addNumber(it) } }
        return out
    }

    fun face(texture: ITexture?, side: Int): JsonArray {
        val out = JsonArray()
        layerIds(texture, side).forEach { out.addNumber(it) }
        return out
    }

    /** `[face(down), face(up), face(north), face(south), face(west), face(east)]` of one texture. */
    fun allFaces(texture: ITexture?): JsonArray = JsonArray().also { a -> for (side in 0 until 6) a.add(face(texture, side)) }

    private fun layerIds(tex: ITexture?, side: Int): IntArray {
        if (tex == null) return EMPTY
        val perSide = cache.getOrPut(tex) { arrayOfNulls(6) }
        perSide[side]?.let { return it }
        val out = ArrayList<Int>(2)
        collect(tex, side, out)
        return out.toIntArray().also { perSide[side] = it }
    }

    private fun collect(tex: ITexture, side: Int, out: MutableList<Int>) {
        when {
            tex === BackgroundMarker -> out.add(intern(JsonObject().apply { addProperty("type", "background") }))
            tex is GTMultiTextureRender -> textureArray(tex, GTMultiTextureRender::class.java)?.forEach {
                if (it != null) out.addAll(layerIds(it, side).asList())
            }
            tex is GTSidedTextureRender -> textureArray(tex, GTSidedTextureRender::class.java)?.getOrNull(side)?.let {
                out.addAll(layerIds(it, side).asList())
            }
            tex is GTRenderedTexture -> out.add(intern(rendered(tex)))
            tex is GTCopiedBlockTextureRender -> out.add(intern(copiedBlock(tex, side, ctm = false)))
            tex.javaClass.name == CTM_CLASS -> out.add(intern(copiedBlock(tex, side, ctm = true)))
            else -> out.add(intern(unknown(tex, side)))
        }
    }

    private fun rendered(tex: GTRenderedTexture): JsonObject {
        val json = JsonObject()
        json.addProperty("type", "icon")
        if (tex.javaClass != GTRenderedTexture::class.java) json.addProperty("class", tex.javaClass.name)
        val container = Reflect.get(tex, GTRenderedTexture::class.java, "mIconContainer") as IIconContainer?
        // GT skips the base icon (not the overlay) when it is the invisible icon
        if (runCatching { container?.icon }.getOrNull() === Textures.InvisibleIcon.INVISIBLE_ICON) json.addProperty("invisible", true)
        for ((k, v) in iconContainer(container).entrySet()) json.add(k, v)
        json.add("rgba", rgba(tex.rgba))
        json.addProperty("glow", Reflect.get(tex, GTRenderedTexture::class.java, "glow") as Boolean? ?: false)
        if (Reflect.get(tex, GTRenderedTexture::class.java, "useExtFacing") == true) json.addProperty("extFacing", true)
        return json
    }

    private fun copiedBlock(tex: ITexture, side: Int, ctm: Boolean): JsonObject {
        val json = JsonObject()
        json.addProperty("type", "block")
        if (ctm) json.addProperty("ctm", true)
        val owner = tex.javaClass
        val block = Reflect.get(tex, owner, "mBlock") as Block?
        val copiedSide = (Reflect.get(tex, owner, "mSide") as Byte?)?.toInt() ?: 6
        val meta = Reflect.get(tex, owner, "mMeta") as Int? ?: 0
        json.addProperty("block", block?.let { Block.blockRegistry.getNameForObject(it) as String? })
        json.addProperty("meta", meta)
        json.addProperty("copiedSide", copiedSide)
        val icon = try {
            block?.getIcon(if (copiedSide == 6) side else copiedSide, meta)
        } catch (t: Exception) {
            // GT itself would crash rendering this (e.g. a casing page entry pointing at an unused meta)
            json.addProperty("iconError", BlockExporter.reason(t))
            null
        }
        addIcon(json, "icon", "key", icon)
        val pass = block?.renderBlockPass ?: 0
        if (pass != 0) json.addProperty("pass", pass)
        return json
    }

    private fun unknown(tex: ITexture, side: Int): JsonObject {
        unknownClasses.merge(tex.javaClass.name, 1, Int::plus)
        val json = JsonObject()
        json.addProperty("type", "unknown")
        json.addProperty("class", tex.javaClass.name)
        if (tex is IIconTexture) addIcon(json, "icon", "key", runCatching { tex.getIcon(side, null as ISBRContext?) }.getOrNull())
        if (tex is IColorModulationContainer) json.add("rgba", rgba(tex.rgba))
        return json
    }

    /** `nameProperty` = raw icon name, `keyProperty` = exported texture key (null if not exportable or invisible). */
    fun addIcon(json: JsonObject, nameProperty: String, keyProperty: String, icon: IIcon?) {
        val name = icon?.iconName
        json.addProperty(nameProperty, name)
        val exportable = name != null && icon !== Textures.InvisibleIcon.INVISIBLE_ICON
        json.addProperty(keyProperty, if (exportable) textures.export(icon) else null)
    }

    /** `icon`/`key`, plus `overlay`/`overlayKey` (rendered untinted on top) and `pass` (if not 0) of an icon container. */
    fun iconContainer(container: IIconContainer?): JsonObject {
        val json = JsonObject()
        addIcon(json, "icon", "key", runCatching { container?.icon }.getOrNull())
        val overlay = runCatching { container?.overlayIcon }.getOrNull()
        if (overlay != null && overlay !== Textures.InvisibleIcon.INVISIBLE_ICON) addIcon(json, "overlay", "overlayKey", overlay)
        val pass = runCatching { container?.renderIconPass }.getOrNull() ?: 0
        if (pass != 0) json.addProperty("pass", pass)
        return json
    }

    companion object {
        private val EMPTY = IntArray(0)
        private const val CTM_CLASS = "gregtech.common.render.GTCopiedCTMBlockTexture"

        fun rgba(values: ShortArray?): JsonArray =
            JsonArray().also { a -> (values ?: shortArrayOf(255, 255, 255, 0)).forEach { a.addNumber(it.toInt()) } }

        @Suppress("UNCHECKED_CAST")
        private fun textureArray(tex: ITexture, owner: Class<*>): Array<ITexture?>? =
            Reflect.get(tex, owner, "mTextures") as Array<ITexture?>?
    }
}

/** Stand-in for the casing background passed to `MTEHatch.getTexturesActive/Inactive`; described as `{"type":"background"}`. */
internal object BackgroundMarker : ITexture {
    override fun renderXPos(ctx: ISBRContext?) {}

    override fun renderXNeg(ctx: ISBRContext?) {}

    override fun renderYPos(ctx: ISBRContext?) {}

    override fun renderYNeg(ctx: ISBRContext?) {}

    override fun renderZPos(ctx: ISBRContext?) {}

    override fun renderZNeg(ctx: ISBRContext?) {}

    override fun isValidTexture() = true
}

/** Declared-field access (GregTech fields are not obfuscated) with per-(class, name) caching. */
internal object Reflect {
    private val fields = HashMap<Pair<Class<*>, String>, Field?>()

    /** First field named [name] declared by [owner] or one of its superclasses, or null. */
    fun field(owner: Class<*>, name: String): Field? =
        fields.getOrPut(owner to name) {
            var c: Class<*>? = owner
            var found: Field? = null
            while (c != null && found == null) {
                found = try {
                    c.getDeclaredField(name).also { it.isAccessible = true }
                } catch (_: NoSuchFieldException) {
                    null
                }
                c = c.superclass
            }
            found
        }

    fun get(obj: Any, owner: Class<*>, name: String): Any? = field(owner, name)?.get(obj)

    fun get(obj: Any, name: String): Any? = get(obj, obj.javaClass, name)

    fun set(obj: Any, owner: Class<*>, name: String, value: Any?): Boolean {
        val f = field(owner, name) ?: return false
        f.set(obj, value)
        return true
    }
}
