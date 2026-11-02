package de.bluecolored.bluemap.gtnh.exporter.oracle

import de.bluecolored.bluemap.gtnh.exporter.FakeWorld
import net.minecraft.block.Block
import net.minecraft.client.Minecraft
import net.minecraft.client.multiplayer.WorldClient
import net.minecraft.client.network.NetHandlerPlayClient
import net.minecraft.network.NetworkManager
import net.minecraft.profiler.Profiler
import net.minecraft.tileentity.TileEntity
import net.minecraft.world.EnumDifficulty
import net.minecraft.world.EnumSkyBlock
import net.minecraft.world.WorldSettings
import net.minecraft.world.WorldType
import net.minecraft.world.biome.BiomeGenBase
import net.minecraftforge.common.util.ForgeDirection

/**
 * The client world (`Minecraft.theWorld`) while [StateCapture] draws: a [WorldClient] (without a connection) that
 * answers every block query from the [FakeWorld] being drawn. Renderers that read the client world instead of the
 * block access they are given (Chisel's CTM renderer reads neighbour metadata through `theWorld`) then see the same
 * single block with air around it, as in a real world.
 */
class StateWorldClient(mc: Minecraft, private val world: FakeWorld) :
    WorldClient(
        NetHandlerPlayClient(mc, null, NetworkManager(true)),
        WorldSettings(0L, WorldSettings.GameType.CREATIVE, false, false, WorldType.FLAT),
        0,
        EnumDifficulty.PEACEFUL,
        Profiler(),
    ) {
    override fun getBlock(x: Int, y: Int, z: Int): Block = world.getBlock(x, y, z)

    override fun getTileEntity(x: Int, y: Int, z: Int): TileEntity? = world.getTileEntity(x, y, z)

    override fun getBlockMetadata(x: Int, y: Int, z: Int): Int = world.getBlockMetadata(x, y, z)

    override fun isAirBlock(x: Int, y: Int, z: Int): Boolean = world.isAirBlock(x, y, z)

    override fun blockExists(x: Int, y: Int, z: Int): Boolean = world.blockExists(x, y, z)

    override fun checkChunksExist(x1: Int, y1: Int, z1: Int, x2: Int, y2: Int, z2: Int): Boolean = true

    override fun doChunksNearChunkExist(x: Int, y: Int, z: Int, radius: Int): Boolean = true

    override fun getBiomeGenForCoords(x: Int, z: Int): BiomeGenBase = world.getBiomeGenForCoords(x, z)

    override fun getLightBrightnessForSkyBlocks(x: Int, y: Int, z: Int, minBlockLight: Int): Int =
        world.getLightBrightnessForSkyBlocks(x, y, z, minBlockLight)

    override fun getLightBrightness(x: Int, y: Int, z: Int): Float = world.getLightBrightness(x, y, z)

    override fun getFullBlockLightValue(x: Int, y: Int, z: Int): Int = world.getFullBlockLightValue(x, y, z)

    override fun getBlockLightValue(x: Int, y: Int, z: Int): Int = world.getBlockLightValue(x, y, z)

    override fun getSavedLightValue(type: EnumSkyBlock, x: Int, y: Int, z: Int): Int = world.getSavedLightValue(type, x, y, z)

    override fun getSkyBlockTypeBrightness(type: EnumSkyBlock, x: Int, y: Int, z: Int): Int =
        world.getSkyBlockTypeBrightness(type, x, y, z)

    override fun canBlockSeeTheSky(x: Int, y: Int, z: Int): Boolean = true

    override fun getHeightValue(x: Int, z: Int): Int = world.getHeightValue(x, z)

    override fun isBlockProvidingPowerTo(x: Int, y: Int, z: Int, direction: Int): Int = 0

    override fun getIndirectPowerLevelTo(x: Int, y: Int, z: Int, direction: Int): Int = 0

    override fun isSideSolid(x: Int, y: Int, z: Int, side: ForgeDirection, default: Boolean): Boolean =
        world.isSideSolid(x, y, z, side, default)

    override fun getHeight(): Int = 256

    override fun extendedLevelsInChunkCache(): Boolean = false
}
