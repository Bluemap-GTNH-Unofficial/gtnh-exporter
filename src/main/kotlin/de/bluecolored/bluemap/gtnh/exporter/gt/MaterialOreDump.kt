package de.bluecolored.bluemap.gtnh.exporter.gt

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import de.bluecolored.bluemap.gtnh.exporter.TextureExporter
import gregtech.api.GregTechAPI
import gregtech.api.enums.Dyes
import gregtech.api.enums.MaterialIconRegistry
import gregtech.api.enums.Materials
import gregtech.api.enums.StoneType
import gregtech.api.enums.TextureSet
import gregtech.api.interfaces.IIconContainer
import gregtech.common.blocks.GTBlockOre
import net.minecraft.block.Block
import java.util.IdentityHashMap

/** `gtnh/gregtech/materials.json` and `gtnh/gregtech/ores.json`. */
internal class MaterialOreDump(private val textures: TextureExporter) {
    private val iconLayers = Layers(textures)

    /** Icon-set key (unique per [TextureSet] instance; normally its `mSetName`) by instance. */
    private val setKeys = IdentityHashMap<TextureSet, String>()
    private val iconSets = JsonObject()
    var materialCount = 0
        private set
    var oreBlockCount = 0
        private set

    private fun setKey(set: TextureSet): String = setKeys.getOrPut(set) {
        var key = set.mSetName
        var n = 2
        while (setKeys.values.contains(key)) key = set.mSetName + "#" + n++
        iconSets.add(key, describeSet(set))
        key
    }

    private fun describeSet(set: TextureSet): JsonObject = JsonObject().apply {
        addProperty("name", set.mSetName)
        if (set.is_custom) addProperty("custom", true)
        val icons = JsonObject()
        for (type in MaterialIconRegistry.IconType.VALUES) {
            if (type.texture == MaterialIconRegistry.TextureType.ITEM || type.suffix == "/void") continue
            icons.add(type.name, icon(set.mTextures.getOrNull(type.ordinal)))
        }
        add("blockIcons", icons)
    }

    private fun icon(container: IIconContainer?): JsonObject = iconLayers.iconContainer(container)

    fun materials(): JsonObject {
        val generated = JsonObject()
        val unindexed = JsonArray()
        val all = LinkedHashSet<Materials>()
        GregTechAPI.sGeneratedMaterials.forEach { if (it != null) all.add(it) }
        all.addAll(Materials.getAll())
        for (mat in all) {
            val id = mat.mMetaItemSubID
            val json = JsonObject().apply {
                addProperty("id", id)
                addProperty("name", mat.mName)
                addProperty("localName", mat.mDefaultLocalName)
                add("rgba", Layers.rgba(mat.mRGBa))
                addProperty("iconSet", mat.mIconSet?.let(::setKey))
                if (mat.hasGlowingOre()) addProperty("glowingOre", true)
            }
            materialCount++
            if (id in 0 until GregTechAPI.sGeneratedMaterials.size && GregTechAPI.sGeneratedMaterials[id] === mat) {
                generated.add(id.toString(), json)
            } else {
                unindexed.add(json)
            }
        }
        val dyes = JsonArray()
        for (dye in Dyes.VALUES) {
            dyes.add(
                JsonObject().apply {
                    addProperty("index", dye.mIndex)
                    addProperty("name", dye.name)
                    addProperty("colorName", dye.mName)
                    add("rgba", Layers.rgba(dye.mRGBa))
                },
            )
        }
        return JsonObject().apply {
            add("materials", generated)
            add("unindexed", unindexed)
            add("iconSets", iconSets)
            add("dyes", dyes)
            add("machineMetal", Layers.rgba(Dyes.MACHINE_METAL.mRGBa))
        }
    }

    /** Must run after [materials] so that every material icon set is known. */
    fun ores(): JsonObject {
        val oreLayers = Layers(textures)
        val blocks = JsonArray()
        val iron = Materials.Iron.mMetaItemSubID
        for (obj in Block.blockRegistry) {
            val block = obj as? GTBlockOre ?: continue
            oreBlockCount++
            val stones = JsonArray()
            val examples = JsonArray()
            block.stoneTypes.forEachIndexed { index, stone ->
                if (stone == null) {
                    stones.add(com.google.gson.JsonNull.INSTANCE)
                    return@forEachIndexed
                }
                stones.add(
                    JsonObject().apply {
                        addProperty("index", index)
                        addProperty("name", stone.name)
                        add("icons", JsonArray().also { a -> for (side in 0 until 6) a.add(JsonObject().also { iconLayers.addIcon(it, "icon", "key", stone.getIcon(side)) }) })
                        add("background", oreLayers.face(stone.getTexture(0), 0))
                    },
                )
                for (small in booleanArrayOf(false, true)) {
                    val meta = index * 1000 + iron + if (small) GTBlockOre.SMALL_ORE_META_OFFSET else 0
                    examples.add(
                        JsonObject().apply {
                            addProperty("meta", meta)
                            add("faces", JsonArray().also { a -> block.getTextures(meta)?.forEachIndexed { side, face -> a.add(oreLayers.face(face, side)) } })
                        },
                    )
                }
            }
            blocks.add(
                JsonObject().apply {
                    addProperty("block", Block.blockRegistry.getNameForObject(block) as String?)
                    add("stoneTypes", stones)
                    add("examples", examples)
                },
            )
        }

        val oreTextures = JsonObject()
        for ((set, key) in setKeys.entries.sortedBy { it.value }) {
            oreTextures.add(
                key,
                JsonObject().apply {
                    add("normal", icon(set.mTextures[MaterialIconRegistry.IconType.ORE.ordinal]))
                    add("small", icon(set.mTextures[MaterialIconRegistry.IconType.ORE_SMALL.ordinal]))
                    if (Reflect.get(set, TextureSet::class.java, "mStoneOreTextures") != null) {
                        val perStone = JsonObject()
                        for (stone in StoneType.VALUES) {
                            perStone.add(
                                stone.name,
                                JsonObject().apply {
                                    add("normal", icon(set.getOreTexture(stone, false)))
                                    add("small", icon(set.getOreTexture(stone, true)))
                                },
                            )
                        }
                        add("perStone", perStone)
                    }
                },
            )
        }

        return JsonObject().apply {
            add(
                "metaEncoding",
                JsonObject().apply {
                    addProperty("materialModulo", 1000)
                    addProperty("stoneDivisor", 1000)
                    addProperty("naturalOffset", GTBlockOre.NATURAL_ORE_META_OFFSET)
                    addProperty("smallOffset", GTBlockOre.SMALL_ORE_META_OFFSET)
                },
            )
            add("layers", oreLayers.table)
            add("blocks", blocks)
            add("oreTextures", oreTextures)
            add("noMaterial", icon(TextureSet.SET_NONE.mTextures[MaterialIconRegistry.IconType.ORE.ordinal]))
            add(
                "exampleMaterial",
                JsonObject().apply {
                    addProperty("id", iron)
                    addProperty("name", Materials.Iron.mName)
                },
            )
        }
    }
}
