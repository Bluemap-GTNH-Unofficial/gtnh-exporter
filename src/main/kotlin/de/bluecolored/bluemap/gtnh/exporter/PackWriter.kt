package de.bluecolored.bluemap.gtnh.exporter

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonPrimitive
import de.bluecolored.bluemap.gtnh.legacy.ResourceKeys
import java.awt.image.BufferedImage
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.io.File
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import javax.imageio.ImageIO

/** Writes the resource pack zip. Every path is written at most once (first write wins). */
class PackWriter(file: File) : Closeable {
    private val zip = ZipOutputStream(BufferedOutputStream(FileOutputStream(file), 1 shl 20))
    private val written = HashSet<String>()

    var files = 0
        private set
    var bytes = 0L
        private set

    fun has(path: String) = path in written

    fun bytes(path: String, data: ByteArray): Boolean {
        if (!written.add(path)) return false
        val entry = ZipEntry(path)
        entry.time = ENTRY_TIME
        zip.putNextEntry(entry)
        zip.write(data)
        zip.closeEntry()
        files++
        bytes += data.size
        return true
    }

    fun json(path: String, json: JsonElement): Boolean = bytes(path, GSON.toJson(json).toByteArray(Charsets.UTF_8))

    fun png(path: String, image: BufferedImage): Boolean {
        if (has(path)) return false
        val out = ByteArrayOutputStream()
        ImageIO.write(image, "png", out)
        return bytes(path, out.toByteArray())
    }

    override fun close() = zip.close()

    companion object {
        val GSON: Gson = GsonBuilder().disableHtmlEscaping().create()
        val PRETTY_GSON: Gson = GsonBuilder().disableHtmlEscaping().setPrettyPrinting().create()

        /** Fixed timestamp for reproducible zips. */
        private const val ENTRY_TIME = 315532800000L // 1980-01-01
    }
}

/** Resource keys of exported files (contract C1: normalization N per registry-name part / path segment). */
object PackKeys {
    /** Vanilla icons used by modded blocks live apart from BlueMap's 1.13 vanilla textures (different names/content). */
    const val LEGACY_MINECRAFT_NAMESPACE = "gtnh_minecraft_1_7"

    /** `(namespace, value)` of a block's blockstate, i.e. of its world [de.bluecolored.bluemap.core.world.BlockState] id. */
    fun blockKey(registryName: String): Pair<String, String> = ResourceKeys.normalizeRegistryName(registryName)

    fun normalizePath(path: String): String = path.split('/').joinToString("/") { ResourceKeys.normalize(it) }

    /** Texture key (`namespace` to `block/<path>`) of a 1.7 block icon name `domain:path`. */
    fun textureKey(iconName: String): Pair<String, String> {
        val colon = iconName.indexOf(':')
        val domain = if (colon < 0) "minecraft" else iconName.substring(0, colon)
        val path = if (colon < 0) iconName else iconName.substring(colon + 1)
        val namespace = if (domain == "minecraft") LEGACY_MINECRAFT_NAMESPACE else ResourceKeys.normalize(domain)
        return namespace to "block/" + normalizePath(path)
    }
}

// Gson on the 1.7.10 compile classpath (2.2.4) has no JsonArray.add(Number/String).
fun JsonArray.addNumber(value: Number) = add(JsonPrimitive(value))

fun JsonArray.addString(value: String?) = add(if (value == null) JsonNull.INSTANCE else JsonPrimitive(value))
