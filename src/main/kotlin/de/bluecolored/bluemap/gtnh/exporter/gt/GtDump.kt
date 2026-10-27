package de.bluecolored.bluemap.gtnh.exporter.gt

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import de.bluecolored.bluemap.gtnh.exporter.BlockExporter
import de.bluecolored.bluemap.gtnh.exporter.ExporterMod
import de.bluecolored.bluemap.gtnh.exporter.PackWriter
import de.bluecolored.bluemap.gtnh.exporter.TextureExporter
import de.bluecolored.bluemap.gtnh.exporter.addString
import gregtech.api.enums.Textures

/**
 * GregTech data for Phase 4 renderers, written under `gtnh/gregtech/` in the pack: `mte.json`, `casings.json`,
 * `materials.json`, `ores.json` (schemas: README, section "GregTech dump").
 */
object GtDump {
    private const val DIR = "gtnh/gregtech/"

    fun dump(pack: PackWriter, textures: TextureExporter): JsonObject {
        val started = System.nanoTime()
        val summary = JsonObject()
        var errors = 0

        fun step(name: String, block: () -> Unit) {
            val t0 = System.nanoTime()
            try {
                block()
            } catch (t: Throwable) {
                errors++
                ExporterMod.log.error("GregTech dump step {} failed", name, t)
                summary.addProperty("${name}Error", BlockExporter.reason(t))
            }
            summary.addProperty("${name}Seconds", (System.nanoTime() - t0) / 1_000_000_000.0)
        }

        step("mte") {
            val mte = MteDump(textures)
            pack.json(DIR + "mte.json", mte.dump())
            errors += mte.failed
            summary.add(
                "mte",
                JsonObject().apply {
                    addProperty("total", mte.total)
                    addProperty("ok", mte.total - mte.failed)
                    addProperty("failed", mte.failed)
                    add("kinds", JsonObject().also { j -> mte.kinds.forEach { (k, v) -> j.addProperty(k, v) } })
                    add(
                        "failedReasons",
                        JsonObject().also { j ->
                            mte.errorReasons.entries.sortedByDescending { it.value }.take(30).forEach { (k, v) -> j.addProperty(k, v) }
                        },
                    )
                    add("unknownTextureClasses", JsonArray().also { a -> mte.unknownTextureClasses.keys.forEach(a::addString) })
                },
            )
        }

        step("casings") {
            val layers = Layers(textures)
            val casings = JsonObject()
            val casingErrors = JsonArray()
            var count = 0
            Textures.BlockIcons.casingTexturePages.forEachIndexed { page, entries ->
                entries?.forEachIndexed { index, texture ->
                    if (texture != null) {
                        val id = (page shl 7) or index
                        try {
                            casings.add(id.toString(), layers.allFaces(texture))
                            count++
                        } catch (t: Throwable) {
                            errors++
                            casingErrors.add(
                                JsonObject().apply {
                                    addProperty("id", id)
                                    addProperty("error", BlockExporter.reason(t))
                                },
                            )
                        }
                    }
                }
            }
            val machineCasings = JsonArray()
            for (tier in Textures.BlockIcons.MACHINE_CASINGS) {
                machineCasings.add(JsonArray().also { colors -> tier.forEach { colors.add(layers.allFaces(it)) } })
            }
            pack.json(
                DIR + "casings.json",
                JsonObject().apply {
                    addProperty("errorTextureId", Textures.BlockIcons.ERROR_TEXTURE_INDEX)
                    add("layers", layers.table)
                    add("casings", casings)
                    add("machineCasings", machineCasings)
                    add("errors", casingErrors)
                },
            )
            summary.addProperty("casings", count)
            summary.addProperty("casingErrors", casingErrors.size())
        }

        val materials = MaterialOreDump(textures)
        step("materials") {
            pack.json(DIR + "materials.json", materials.materials())
            summary.addProperty("materials", materials.materialCount)
        }
        step("ores") {
            pack.json(DIR + "ores.json", materials.ores())
            summary.addProperty("oreBlocks", materials.oreBlockCount)
        }

        summary.addProperty("errors", errors)
        summary.addProperty("seconds", (System.nanoTime() - started) / 1_000_000_000.0)
        ExporterMod.log.info("GregTech dump: {}", PackWriter.GSON.toJson(summary))
        return summary
    }
}
