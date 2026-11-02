package de.bluecolored.bluemap.gtnh.exporter.oracle

import gregtech.api.GregTechAPI
import gregtech.api.interfaces.tileentity.IGregTechTileEntity
import gregtech.api.metatileentity.BaseMetaPipeEntity
import gregtech.api.metatileentity.BaseMetaTileEntity
import gregtech.api.metatileentity.MetaPipeEntity
import net.minecraft.tileentity.TileEntity

/**
 * GregTech machines as oracle states. The exporter lists `gregtech:gt.blockmachines` with the MTE id as its meta
 * (item damage); in a world the block meta is the MTE's base type and the id is in the tile entity. Only touched
 * when GregTech is loaded.
 */
object StateGregTech {
    const val MACHINES = "gregtech:gt.blockmachines"

    /** `(block meta, tile entity)` of MTE [id], built the way GT places one (as `gt/MteDump.kt`), or null if unknown. */
    fun machine(id: Int): Pair<Int, TileEntity>? {
        val proto = GregTechAPI.METATILEENTITIES.getOrNull(id) ?: return null
        val baseType = proto.tileEntityBaseType.toInt()
        val base = GregTechAPI.createTileEntity(baseType) as? IGregTechTileEntity
            ?: if (proto is MetaPipeEntity) BaseMetaPipeEntity() else BaseMetaTileEntity()
        base.setInitialValuesAsNBT(null, id.toShort())
        return baseType to (base as TileEntity)
    }
}
