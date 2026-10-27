package de.bluecolored.bluemap.gtnh.exporter.gt

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.gtnewhorizon.structurelib.alignment.IAlignmentProvider
import de.bluecolored.bluemap.gtnh.exporter.BlockExporter
import de.bluecolored.bluemap.gtnh.exporter.TextureExporter
import de.bluecolored.bluemap.gtnh.exporter.addNumber
import gregtech.GTMod
import gregtech.api.GregTechAPI
import gregtech.api.enums.Materials
import gregtech.api.enums.Textures
import gregtech.api.interfaces.ITexture
import gregtech.api.interfaces.metatileentity.IMetaTileEntity
import gregtech.api.interfaces.tileentity.IGregTechTileEntity
import gregtech.api.metatileentity.BaseMetaPipeEntity
import gregtech.api.metatileentity.BaseMetaTileEntity
import gregtech.api.metatileentity.MetaPipeEntity
import gregtech.api.metatileentity.implementations.MTEBasicBatteryBuffer
import gregtech.api.metatileentity.implementations.MTEBasicGenerator
import gregtech.api.metatileentity.implementations.MTEBasicHull
import gregtech.api.metatileentity.implementations.MTEBasicMachine
import gregtech.api.metatileentity.implementations.MTEBasicTank
import gregtech.api.metatileentity.implementations.MTECable
import gregtech.api.metatileentity.implementations.MTEFluidPipe
import gregtech.api.metatileentity.implementations.MTEFrame
import gregtech.api.metatileentity.implementations.MTEHatch
import gregtech.api.metatileentity.implementations.MTEItemPipe
import gregtech.api.metatileentity.implementations.MTEMultiBlockBase
import gregtech.api.metatileentity.implementations.MTETieredMachineBlock
import gregtech.api.metatileentity.implementations.MTETransformer
import gregtech.api.render.ISBRWorldContext
import gregtech.common.render.IMTERenderer
import net.minecraftforge.common.util.ForgeDirection
import java.util.TreeMap

/** `gtnh/gregtech/mte.json`: every registered MetaTileEntity with its face textures. */
internal class MteDump(textures: TextureExporter) {
    private val layers = Layers(textures)

    /** Interned faces: a face is a list of [layers] indices, bottom layer first. */
    private val faceTable = JsonArray()
    private val faceIndex = HashMap<String, Int>()
    private val classes = TreeMap<String, String?>()
    private val errors = JsonArray()
    val errorReasons = TreeMap<String, Int>()
    val kinds = TreeMap<String, Int>()
    var total = 0
        private set
    var failed = 0
        private set

    val unknownTextureClasses get() = layers.unknownClasses

    fun dump(): JsonObject {
        val entries = JsonArray()
        val registry = GregTechAPI.METATILEENTITIES
        for (id in 1 until registry.size) {
            val proto = registry[id] ?: continue
            total++
            val entry = JsonObject()
            try {
                describe(id, proto, entry)
            } catch (t: Throwable) {
                fail(id, entry, t)
            }
            entries.add(entry)
        }
        return JsonObject().apply {
            add(
                "render",
                JsonObject().apply {
                    addProperty("fixedBottomFaceUV", GTMod.clientProxy().fixedBottomFaceUV)
                    addProperty("renderGlowTextures", GTMod.proxy.mRenderGlowTextures)
                },
            )
            add("layers", layers.table)
            add("faces", faceTable)
            add(
                "classes",
                JsonObject().also { json -> classes.forEach { (k, v) -> json.addProperty(k, v) } },
            )
            add("entries", entries)
            add("errors", errors)
        }
    }

    private fun faceId(face: JsonArray): Int =
        faceIndex.getOrPut(face.toString()) {
            faceTable.add(face)
            faceTable.size() - 1
        }

    private fun faceId(layerArray: Array<ITexture?>?, side: ForgeDirection) = faceId(layers.face(layerArray, side.ordinal))

    private fun fail(id: Int, entry: JsonObject, t: Throwable) {
        failed++
        val reason = BlockExporter.reason(t)
        errorReasons.merge(reason, 1, Int::plus)
        entry.addProperty("error", reason)
        errors.add(
            JsonObject().apply {
                addProperty("id", id)
                addProperty("error", reason)
                t.stackTrace.firstOrNull()?.let { addProperty("at", it.toString()) }
            },
        )
    }

    private fun describe(id: Int, proto: IMetaTileEntity, entry: JsonObject) {
        val kind = kind(proto)
        kinds.merge(kind, 1, Int::plus)
        entry.addProperty("id", id)
        entry.addProperty("name", proto.metaName)
        entry.addProperty("unlocalizedName", runCatching { proto.localNameKey }.getOrNull())
        entry.addProperty("class", registerClass(proto.javaClass))
        entry.addProperty("kind", kind)
        entry.addProperty("baseType", proto.tileEntityBaseType.toInt())
        if (proto is MTETieredMachineBlock) entry.addProperty("tier", proto.mTier.toInt())
        if (proto is IAlignmentProvider) entry.addProperty("alignment", true)
        if (proto is IMTERenderer) entry.addProperty("tesr", true)
        customRenderer(proto.javaClass)?.let { entry.addProperty("customRender", it) }

        // the way GT builds a placed machine: base TE for the MTE's block meta, then a fresh MTE copy bound to it
        val base = GregTechAPI.createTileEntity(proto.tileEntityBaseType.toInt()) as? IGregTechTileEntity
            ?: if (proto is MetaPipeEntity) BaseMetaPipeEntity() else BaseMetaTileEntity()
        base.setInitialValuesAsNBT(null, id.toShort())
        val mte = base.metaTileEntity ?: error("newMetaEntity returned no MetaTileEntity")

        if (mte is MetaPipeEntity) {
            entry.add("pipe", pipe(base, mte))
            return
        }
        val textures = JsonArray()
        for (facing in SIDES) {
            textures.add(JsonArray().also { states -> for (active in STATES) states.add(faces(base, mte, facing, active)) })
        }
        entry.add("textures", textures)
        if (mte is MTEBasicMachine) entry.add("outputFace", outputFaces(base, mte))
        if (mte is MTEHatch) entry.add("hatch", hatch(mte))
    }

    /** Face ids `[down, up, north, south, west, east]` for front facing [facing] (basic machines: main facing too). */
    private fun faces(base: IGregTechTileEntity, mte: IMetaTileEntity, facing: ForgeDirection, active: Boolean): JsonArray {
        if (mte is MTEBasicMachine) mte.mMainFacing = facing
        setFacing(base, facing)
        setActive(base, active)
        val out = JsonArray()
        for (side in SIDES) out.addNumber(faceId(mte.getTexture(base, side, facing, COLOR_NONE, active, false), side))
        return out
    }

    /**
     * Basic machines: main (front) facing NORTH and the separate output facing (`mFacing`) DOWN, UP or SOUTH; the face
     * id of the output side (GT only distinguishes down / up / horizontal output sides).
     */
    private fun outputFaces(base: IGregTechTileEntity, mte: MTEBasicMachine): JsonArray {
        val out = JsonArray()
        for (active in STATES) {
            val json = JsonObject()
            for ((name, side) in OUTPUT_SIDES) {
                mte.mMainFacing = ForgeDirection.NORTH
                setFacing(base, side)
                setActive(base, active)
                json.addProperty(name, faceId(mte.getTexture(base, side, side, COLOR_NONE, active, false), side))
            }
            out.add(json)
        }
        return out
    }

    /** Front overlays with the casing background as `{"type":"background"}`, plus the default background per side. */
    private fun hatch(mte: MTEHatch): JsonObject = JsonObject().apply {
        add(
            "front",
            JsonArray().also {
                it.addNumber(faceId(mte.getTexturesInactive(BackgroundMarker), ForgeDirection.NORTH))
                it.addNumber(faceId(mte.getTexturesActive(BackgroundMarker), ForgeDirection.NORTH))
            },
        )
        val background = Textures.BlockIcons.MACHINE_CASINGS[mte.mTier.toInt()][COLOR_NONE + 1]
        add("defaultBackground", JsonArray().also { a -> for (side in SIDES) a.addNumber(faceId(layers.face(background, side.ordinal))) })
    }

    private fun pipe(base: IGregTechTileEntity, mte: MetaPipeEntity): JsonObject {
        val json = JsonObject()
        json.addProperty("thickness", mte.thickness)
        runCatching { mte.javaClass.getMethod("getCollisionThickness").invoke(mte) as Float }
            .getOrNull()?.let { json.addProperty("collisionThickness", it) }
        material(mte)?.let { json.add("material", it) }
        when (mte) {
            is MTECable -> json.addProperty("insulated", mte.mInsulated)
            is MTEFluidPipe -> json.addProperty("fluidChannels", mte.mPipeAmount)
        }
        // straight west-east pipe, as GT renders it in inventories: end faces (connected) and side faces
        val straight = ForgeDirection.WEST.flag or ForgeDirection.EAST.flag
        (base as? BaseMetaPipeEntity)?.mConnections = straight.toByte()
        json.addProperty("connected", faceId(mte.getTexture(base, ForgeDirection.WEST, straight, COLOR_NONE, true, false), ForgeDirection.WEST))
        json.addProperty("unconnected", faceId(mte.getTexture(base, ForgeDirection.DOWN, straight, COLOR_NONE, false, false), ForgeDirection.DOWN))
        return json
    }

    private fun material(mte: Any): JsonObject? {
        val value = Reflect.get(mte, "mMaterial") ?: Reflect.get(mte, "mPipeMaterial") ?: return null
        return JsonObject().apply {
            if (value is Materials) {
                addProperty("name", value.mName)
                addProperty("id", value.mMetaItemSubID)
                add("rgba", Layers.rgba(value.mRGBa))
                addProperty("iconSet", value.mIconSet?.mSetName)
            } else {
                addProperty("name", (Reflect.get(value, "mName") ?: Reflect.get(value, "unlocalizedName") ?: value).toString())
                addProperty("class", value.javaClass.name)
                (Reflect.get(value, "mRGBa") ?: Reflect.get(value, "RGBA"))?.let { if (it is ShortArray) add("rgba", Layers.rgba(it)) }
            }
        }
    }

    private fun registerClass(cls: Class<*>): String {
        var c: Class<*>? = cls
        while (c != null && c != Any::class.java && c.name !in classes) {
            classes[c.name] = c.superclass?.takeIf { it != Any::class.java }?.name
            c = c.superclass
        }
        return cls.name
    }

    companion object {
        private val SIDES = ForgeDirection.VALID_DIRECTIONS
        private val STATES = booleanArrayOf(false, true)
        private val OUTPUT_SIDES = listOf("down" to ForgeDirection.DOWN, "up" to ForgeDirection.UP, "side" to ForgeDirection.SOUTH)
        private const val COLOR_NONE = -1

        private val mFacing = Reflect.field(BaseMetaTileEntity::class.java, "mFacing")
        private val mActive = Reflect.field(BaseMetaTileEntity::class.java, "mActive")

        private fun setFacing(base: IGregTechTileEntity, facing: ForgeDirection) {
            if (base is BaseMetaTileEntity) mFacing?.set(base, facing)
        }

        private fun setActive(base: IGregTechTileEntity, active: Boolean) {
            if (base is BaseMetaTileEntity) mActive?.set(base, active)
        }

        private val DEFAULT_RENDERERS = setOf(
            "gregtech.api.metatileentity.CommonMetaTileEntity",
            "gregtech.api.metatileentity.MetaTileEntity",
            "gregtech.api.metatileentity.MetaPipeEntity",
        )

        /** Class declaring `renderInWorld` if it is not one of GT's default implementations. */
        private fun customRenderer(cls: Class<*>): String? = runCatching {
            cls.getMethod("renderInWorld", ISBRWorldContext::class.java).declaringClass.name
        }.getOrNull()?.takeIf { it !in DEFAULT_RENDERERS }

        fun kind(mte: IMetaTileEntity): String = when (mte) {
            is MTECable -> "cable"
            is MTEFluidPipe -> "fluid_pipe"
            is MTEItemPipe -> "item_pipe"
            is MTEFrame -> "frame"
            is MetaPipeEntity -> "pipe"
            is MTEHatch -> "hatch"
            is MTEMultiBlockBase -> "multiblock_controller"
            is MTEBasicMachine -> "basic_machine"
            is MTEBasicGenerator -> "generator"
            is MTETransformer -> "transformer"
            is MTEBasicBatteryBuffer -> "battery_buffer"
            is MTEBasicHull -> "hull"
            is MTEBasicTank -> "tank"
            is MTETieredMachineBlock -> "tiered_machine"
            else -> "machine"
        }
    }
}

