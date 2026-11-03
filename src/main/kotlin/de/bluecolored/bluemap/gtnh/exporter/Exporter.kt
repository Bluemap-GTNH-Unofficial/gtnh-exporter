package de.bluecolored.bluemap.gtnh.exporter

import com.google.gson.JsonObject
import cpw.mods.fml.common.Loader
import de.bluecolored.bluemap.gtnh.exporter.gt.GtDump
import net.minecraft.client.Minecraft
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** Writes `bluemap-gtnh-resources-<pack version>.zip` and `export-report.json` into [outDir]. */
class Exporter(private val mc: Minecraft, private val outDir: File, private val extraStatesFile: File?) {
    fun run() {
        val started = System.nanoTime()
        outDir.mkdirs()
        // `-Dbluemap.gtnh.exporter.packSuffix=modernity` -> bluemap-gtnh-resources-<version>-modernity.zip
        val suffix = System.getProperty("bluemap.gtnh.exporter.packSuffix")?.takeIf { it.isNotBlank() }?.let { "-$it" } ?: ""
        val packVersion = packVersion(Loader.instance().configDir)
        val zip = File(outDir, "bluemap-gtnh-resources-$packVersion$suffix.zip")
        val partial = File(outDir, zip.name + ".part")
        val extraStates = BlockExporter.parseExtraStates(extraStatesFile)
        ExporterMod.log.info("Exporting to {} ({} extra states)", zip, extraStates.values.sumOf { it.size })

        val report = JsonObject()
        PackWriter(partial).use { pack ->
            pack.json(
                "pack.mcmeta",
                JsonObject().apply {
                    add(
                        "pack",
                        JsonObject().apply {
                            addProperty("pack_format", 4)
                            addProperty("description", "GT New Horizons $packVersion modded blocks for BlueMap (generated)")
                        },
                    )
                },
            )

            val textures = TextureExporter(mc, pack)
            val modData = ModData(mc, pack, textures)
            val blocks = BlockExporter(mc, pack, textures, extraStates, modData)
            // debugging aid for the GregTech dump: `-Dbluemap.gtnh.exporter.skipBlocks=true`
            if (System.getProperty("bluemap.gtnh.exporter.skipBlocks") != "true") blocks.exportAll()
            modData.writeChisel()
            pack.json(
                "gtnh/mods/myctmlib.json",
                JsonObject().apply {
                    addProperty("format", 1)
                    add("textures", JsonObject().also { o -> textures.myCtm.forEach { (k, v) -> o.add(k, v) } })
                },
            )
            val modAssets = try {
                modData.copyAssets()
            } catch (t: Throwable) {
                ExporterMod.log.error("Copying mod assets failed", t)
                -1
            }
            val aspects = try {
                modData.writeAspects()
            } catch (t: Throwable) {
                ExporterMod.log.error("Writing Thaumcraft aspects failed", t)
                -1
            }
            report.add(
                "modData",
                JsonObject().apply {
                    addProperty("chiselStates", modData.chiselStates)
                    addProperty("myctmlibTextures", textures.myCtm.size)
                    addProperty("assets", modAssets)
                    addProperty("aspects", aspects)
                },
            )
            val blocksDone = System.nanoTime()
            ExporterMod.log.info("Blocks exported in {} s", (blocksDone - started) / 1_000_000_000.0)

            val gt = if (Loader.isModLoaded("gregtech")) {
                try {
                    GtDump.dump(pack, textures)
                } catch (t: Throwable) {
                    ExporterMod.log.error("GregTech dump failed", t)
                    JsonObject().apply { addProperty("error", BlockExporter.reason(t)) }
                }
            } else {
                null
            }

            val mods = JsonObject()
            val total = BlockExporter.ModStats()
            for ((modid, stats) in blocks.mods) {
                mods.add(modid, stats.toJson())
                total.blocks += stats.blocks
                total.states += stats.states
                total.cube += stats.cube
                total.quads += stats.quads
                total.bbox += stats.bbox
                total.failed += stats.failed
                stats.failedReasons.forEach { (k, v) -> total.failedReasons.merge(k, v, Int::plus) }
            }
            val totalJson = total.toJson().apply { remove("failedStates") }

            report.addProperty("packVersion", packVersion)
            report.addProperty("zip", zip.name)
            report.add("total", totalJson)
            report.add(
                "textures",
                JsonObject().apply {
                    textures.sourceCounts.forEach { (k, v) -> addProperty(k.name.lowercase(), v) }
                    addProperty("atlasSprites", textures.sprites.size)
                    addProperty("unmappedQuads", blocks.unmappedQuads)
                    add("failures", com.google.gson.JsonArray().also { a -> textures.failures.take(200).forEach(a::addString) })
                },
            )
            gt?.let { report.add("gregtech", it) }
            report.add("mods", mods)
            report.addProperty("packFiles", pack.files + 1)
            report.addProperty("seconds", (System.nanoTime() - started) / 1_000_000_000.0)
            pack.bytes("gtnh/export-report.json", PackWriter.PRETTY_GSON.toJson(report).toByteArray(Charsets.UTF_8))
        }
        Files.move(partial.toPath(), zip.toPath(), StandardCopyOption.REPLACE_EXISTING)
        File(outDir, "export-report.json").writeText(PackWriter.PRETTY_GSON.toJson(report))
        ExporterMod.log.info("Export written: {} ({} s)", zip, (System.nanoTime() - started) / 1_000_000_000.0)
    }

    companion object {
        const val PACK_VERSION_PROPERTY = "bluemap.gtnh.exporter.packVersion"
        private val MOD_PACK_VERSION = Regex("""^\s*S:ModPackVersion=(\S+)\s*$""", RegexOption.MULTILINE)

        /**
         * The GTNH pack version in the pack's name: `-Dbluemap.gtnh.exporter.packVersion=<version>` if set, else the
         * version the pack reports itself (`S:ModPackVersion` in GTNH Core Mod's `config/GTNewHorizons/dreamcraft.cfg`).
         */
        fun packVersion(configDir: File): String {
            System.getProperty(PACK_VERSION_PROPERTY)?.takeIf { it.isNotBlank() }?.let { return it }
            val config = File(configDir, "GTNewHorizons/dreamcraft.cfg")
            val version = if (config.isFile) MOD_PACK_VERSION.find(config.readText())?.groupValues?.get(1) else null
            return version ?: error("unknown GTNH pack version: no S:ModPackVersion in $config; set -D$PACK_VERSION_PROPERTY")
        }
    }
}
