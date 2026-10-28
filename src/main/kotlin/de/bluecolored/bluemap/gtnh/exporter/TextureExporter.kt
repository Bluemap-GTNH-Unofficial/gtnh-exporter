package de.bluecolored.bluemap.gtnh.exporter

import cpw.mods.fml.relauncher.ReflectionHelper
import net.minecraft.client.Minecraft
import net.minecraft.client.renderer.texture.TextureAtlasSprite
import net.minecraft.client.renderer.texture.TextureMap
import net.minecraft.util.IIcon
import net.minecraft.util.ResourceLocation
import org.lwjgl.BufferUtils
import org.lwjgl.opengl.GL11
import org.lwjgl.opengl.GL12
import java.awt.image.BufferedImage
import java.io.IOException
import java.nio.IntBuffer
import javax.imageio.ImageIO

/**
 * Exports block-atlas sprites as pack textures (`assets/<ns>/textures/block/<path>.png`, see [PackKeys.textureKey]).
 *
 * Pixel sources, in order: the sprite's PNG through the resource manager (honours resource packs; `.mcmeta`
 * animations are copied, animation strips without one are cut to their first frame), the sprite's retained frame
 * data, and finally a crop of the GL atlas read back once (sprites with custom loaders or generated pixels).
 */
class TextureExporter(private val mc: Minecraft, private val pack: PackWriter) {
    enum class Source { RESOURCE, FRAME_DATA, ATLAS, FAILED }

    private val atlas: TextureMap = mc.textureMapBlocks
    private val exported = HashMap<String, String?>()
    val sourceCounts = java.util.EnumMap<Source, Int>(Source::class.java)
    val failures = ArrayList<String>()

    /** MyCTMLib connected textures (`myctmlib` section of a texture's `.mcmeta`, e.g. from Modernity-GTNH), by texture key. */
    val myCtm = java.util.TreeMap<String, com.google.gson.JsonObject>()

    /** Connected-texture sheets: tall or wide images that are not animation strips. */
    private val ctmSheets = HashSet<String>()

    /** All uploaded block sprites by icon name. */
    val sprites: Map<String, TextureAtlasSprite> = run {
        @Suppress("UNCHECKED_CAST")
        val map = ReflectionHelper.getPrivateValue<Map<String, TextureAtlasSprite>, TextureMap>(
            TextureMap::class.java,
            atlas,
            "mapUploadedSprites",
            "field_94252_e",
        )
        HashMap(map)
    }

    private val spriteGrid = SpriteGrid(sprites.values)
    private var atlasPixels: IntBuffer? = null
    private var atlasWidth = 0

    /** The sprite whose atlas rectangle contains (u, v), or null. */
    fun spriteAt(u: Float, v: Float): TextureAtlasSprite? = spriteGrid.find(u, v)

    /** Exports the icon (if not yet done) and returns its texture key `ns:block/path`, or null if it has no pixels. */
    fun export(icon: IIcon?): String? {
        if (icon == null) return null
        val name = icon.iconName ?: return null
        if (isErrorIcon(name)) return null
        val sprite = sprites[name] ?: (icon as? TextureAtlasSprite)
        return export(name, sprite)
    }

    fun export(iconName: String): String? = if (isErrorIcon(iconName)) null else export(iconName, sprites[iconName])

    private fun export(name: String, sprite: TextureAtlasSprite?): String? {
        // failed icons are cached as null too
        if (name in exported) return exported[name]
        val (namespace, value) = PackKeys.textureKey(name)
        val path = "assets/$namespace/textures/$value.png"
        val source = try {
            write(name, sprite, path)
        } catch (ex: Exception) {
            failures.add("$name: $ex")
            Source.FAILED
        }
        sourceCounts.merge(source, 1, Int::plus)
        val key = if (source == Source.FAILED) null else "$namespace:$value"
        exported[name] = key
        return key
    }

    private fun write(name: String, sprite: TextureAtlasSprite?, path: String): Source {
        val colon = name.indexOf(':')
        val domain = if (colon < 0) "minecraft" else name.substring(0, colon)
        val iconPath = if (colon < 0) name else name.substring(colon + 1)
        val location = ResourceLocation(domain, "textures/blocks/$iconPath.png")

        if (sprite == null || !sprite.hasCustomLoader(mc.resourceManager, location)) {
            val image = try {
                mc.resourceManager.getResource(location).inputStream.use { ImageIO.read(it) }
            } catch (_: IOException) {
                null
            }
            if (image != null) {
                val mcmeta = try {
                    mc.resourceManager.getResource(ResourceLocation(domain, "textures/blocks/$iconPath.png.mcmeta"))
                        .inputStream.use { it.readBytes() }
                } catch (_: IOException) {
                    null
                }
                mcmeta?.let { recordCtm(name, it) }
                val animation = mcmeta?.let(::sanitizeMcmeta)
                if (animation != null) {
                    pack.png(path, image)
                    pack.bytes("$path.mcmeta", animation)
                } else if (name !in ctmSheets && image.height > image.width && image.height % image.width == 0) {
                    pack.png(path, image.getSubimage(0, 0, image.width, image.width))
                } else {
                    pack.png(path, image)
                }
                return Source.RESOURCE
            }
        }

        if (sprite == null) {
            failures.add("$name: no sprite and no resource")
            return Source.FAILED
        }

        if (sprite.frameCount > 0) {
            val frame = sprite.getFrameTextureData(0)
            if (frame != null && frame.isNotEmpty() && frame[0] != null) {
                val pixels = frame[0]
                val w = sprite.iconWidth
                val h = sprite.iconHeight
                if (w > 0 && h > 0 && pixels.size >= w * h) {
                    val image = BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB)
                    image.setRGB(0, 0, w, h, pixels, 0, w)
                    pack.png(path, image)
                    return Source.FRAME_DATA
                }
            }
        }

        val image = cropAtlas(sprite) ?: run {
            failures.add("$name: empty atlas region")
            return Source.FAILED
        }
        pack.png(path, image)
        return Source.ATLAS
    }

    private fun cropAtlas(sprite: TextureAtlasSprite): BufferedImage? {
        val pixels = atlasPixels ?: readAtlas().also { atlasPixels = it }
        val w = sprite.iconWidth
        val h = sprite.iconHeight
        if (w <= 0 || h <= 0) return null
        val image = BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB)
        for (y in 0 until h) for (x in 0 until w) {
            image.setRGB(x, y, pixels.get((sprite.originY + y) * atlasWidth + sprite.originX + x))
        }
        return image
    }

    /**
     * Records the `myctmlib` section of a 1.7 `.mcmeta` (MyCTMLib connected textures): the referenced `connection` and
     * `alt` sheets and `equivalents` are exported as textures and replaced by their texture keys; other fields are copied.
     * `method` defaults to `compact` (MyCTMLib's default).
     */
    private fun recordCtm(iconName: String, bytes: ByteArray) {
        val section = try {
            val reader = com.google.gson.stream.JsonReader(java.io.StringReader(String(bytes, Charsets.UTF_8)))
            reader.isLenient = true
            com.google.gson.internal.Streams.parse(reader).asJsonObject.getAsJsonObject("myctmlib")
        } catch (_: Exception) {
            null
        } ?: return
        val (namespace, value) = PackKeys.textureKey(iconName)
        val entry = com.google.gson.JsonObject()
        entry.addProperty("method", section.get("method")?.takeIf { it.isJsonPrimitive }?.asString ?: "compact")
        for ((k, v) in section.entrySet()) {
            when (k) {
                "method" -> {}
                "connection", "alt" -> v.takeIf { it.isJsonPrimitive }?.asString?.let { sheet ->
                    ctmSheets.add(sheet)
                    export(sheet)
                }?.let { entry.addProperty(k, it) }
                "equivalents" -> if (v.isJsonArray) {
                    val keys = com.google.gson.JsonArray()
                    v.asJsonArray.filter { it.isJsonPrimitive }.mapNotNull { export(it.asString) }.forEach(keys::addString)
                    entry.add(k, keys)
                }
                else -> entry.add(k, v)
            }
        }
        if (!entry.has("connection")) failures.add("$iconName: myctmlib connection sheet not found")
        myCtm["$namespace:$value"] = entry
    }

    private fun readAtlas(): IntBuffer {
        GL11.glBindTexture(GL11.GL_TEXTURE_2D, atlas.glTextureId)
        val width = GL11.glGetTexLevelParameteri(GL11.GL_TEXTURE_2D, 0, GL11.GL_TEXTURE_WIDTH)
        val height = GL11.glGetTexLevelParameteri(GL11.GL_TEXTURE_2D, 0, GL11.GL_TEXTURE_HEIGHT)
        ExporterMod.log.info("Reading back the block atlas ({}x{})", width, height)
        val buffer = BufferUtils.createIntBuffer(width * height)
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1)
        GL11.glGetTexImage(GL11.GL_TEXTURE_2D, 0, GL12.GL_BGRA, GL12.GL_UNSIGNED_INT_8_8_8_8_REV, buffer)
        atlasWidth = width
        return buffer
    }

    companion object {
        /** Angelica/MCPatcher connected-texture tiles (`<domain>:mcpatcher/ctm/<set>/<n>.png`), chosen by neighbours. */
        fun isCtmTile(name: String?): Boolean = name != null && name.substringAfter(':').startsWith("mcpatcher/ctm/")

        /**
         * Placeholders the client substitutes for icons that do not exist: vanilla `missingno`, Forge's
         * `MISSING_ICON_BLOCK_<id>_<name>` and Angelica's `error_block_<n>` (returned instead of a null icon).
         * Exporting them would paint whole blocks magenta/black or with Angelica's red face.
         */
        fun isErrorIcon(name: String?): Boolean {
            if (name == null) return true
            val path = name.substringAfter(':')
            return path == "missingno" || path.startsWith("MISSING_ICON_") ||
                (name.startsWith("angelica:") && path.startsWith("error_block"))
        }

        /**
         * Re-serializes a 1.7 `.mcmeta` leniently (some mods ship trailing commas, which 1.7's Gson tolerated but
         * BlueMap's strict parser does not, dropping the whole texture); array holes become nothing.
         * Returns null if it cannot be read or has no `animation` section.
         */
        fun sanitizeMcmeta(bytes: ByteArray): ByteArray? {
            val json = try {
                val reader = com.google.gson.stream.JsonReader(java.io.StringReader(String(bytes, Charsets.UTF_8)))
                reader.isLenient = true
                com.google.gson.internal.Streams.parse(reader)
            } catch (_: Exception) {
                return null
            }
            if (!json.isJsonObject || !json.asJsonObject.has("animation")) return null
            return PackWriter.GSON.toJson(dropNulls(json)).toByteArray(Charsets.UTF_8)
        }

        private fun dropNulls(element: com.google.gson.JsonElement): com.google.gson.JsonElement = when {
            element.isJsonArray -> com.google.gson.JsonArray().also { out ->
                element.asJsonArray.filterNot { it.isJsonNull }.forEach { out.add(dropNulls(it)) }
            }
            element.isJsonObject -> com.google.gson.JsonObject().also { out ->
                element.asJsonObject.entrySet().forEach { (k, v) -> if (!v.isJsonNull) out.add(k, dropNulls(v)) }
            }
            else -> element
        }
    }

    /** Uniform grid over atlas uv space for sprite lookup by uv. */
    private class SpriteGrid(sprites: Collection<TextureAtlasSprite>) {
        private val size = 256
        private val cells = arrayOfNulls<ArrayList<TextureAtlasSprite>>(size * size)

        init {
            for (sprite in sprites) {
                if (sprite.maxU <= sprite.minU || sprite.maxV <= sprite.minV) continue
                val x0 = cell(sprite.minU)
                val x1 = cell(sprite.maxU)
                val y0 = cell(sprite.minV)
                val y1 = cell(sprite.maxV)
                for (y in y0..y1) for (x in x0..x1) {
                    (cells[y * size + x] ?: ArrayList<TextureAtlasSprite>(2).also { cells[y * size + x] = it }).add(sprite)
                }
            }
        }

        private fun cell(f: Float) = (f * size).toInt().coerceIn(0, size - 1)

        fun find(u: Float, v: Float): TextureAtlasSprite? {
            val list = cells[cell(v) * size + cell(u)] ?: return null
            var best: TextureAtlasSprite? = null
            for (s in list) {
                if (u >= s.minU && u <= s.maxU && v >= s.minV && v <= s.maxV) {
                    // prefer the smallest containing rectangle (sprites never overlap, but edges touch)
                    if (best == null || (s.maxU - s.minU) * (s.maxV - s.minV) < (best.maxU - best.minU) * (best.maxV - best.minV)) {
                        best = s
                    }
                }
            }
            return best
        }
    }
}
