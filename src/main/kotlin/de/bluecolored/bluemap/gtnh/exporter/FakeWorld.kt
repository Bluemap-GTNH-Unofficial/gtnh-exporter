package de.bluecolored.bluemap.gtnh.exporter

import net.minecraft.block.Block
import net.minecraft.client.multiplayer.ChunkProviderClient
import net.minecraft.entity.Entity
import net.minecraft.init.Blocks
import net.minecraft.profiler.Profiler
import net.minecraft.tileentity.TileEntity
import net.minecraft.world.EnumSkyBlock
import net.minecraft.world.World
import net.minecraft.world.WorldProvider
import net.minecraft.world.WorldSettings
import net.minecraft.world.WorldType
import net.minecraft.world.biome.BiomeGenBase
import net.minecraft.world.chunk.IChunkProvider
import net.minecraft.world.storage.SaveHandlerMP
import net.minecraftforge.common.util.ForgeDirection

/**
 * A client-side world containing a single block (with its tile entity, if any) at [X], [Y], [Z], surrounded by air,
 * full sky light and one biome everywhere. It is a real [World] (not just an IBlockAccess) because many renderers
 * and tile entities cast to or call into `World`. Neighbour-dependent renderers (connected textures, fences, pipes)
 * therefore see their unconnected base state. Nothing is ticked, saved or networked.
 */
class FakeWorld private constructor(provider: WorldProvider) :
    World(SaveHandlerMP(), "bluemap-gtnh-export", provider, SETTINGS, Profiler()) {

    var block: Block = Blocks.air
    var meta: Int = 0
    var tileEntity: TileEntity? = null
    var biome: BiomeGenBase = BiomeGenBase.plains

    init {
        isRemote = true
        provider.registerWorld(this)
        chunkProvider = createChunkProvider()
    }

    fun set(block: Block, meta: Int, tileEntity: TileEntity?) {
        this.block = block
        this.meta = meta
        this.tileEntity = tileEntity
    }

    private fun isCenter(x: Int, y: Int, z: Int) = x == X && y == Y && z == Z

    override fun createChunkProvider(): IChunkProvider = ChunkProviderClient(this)

    override fun func_152379_p(): Int = 2

    override fun getEntityByID(id: Int): Entity? = null

    override fun getBlock(x: Int, y: Int, z: Int): Block = if (isCenter(x, y, z)) block else Blocks.air

    override fun getTileEntity(x: Int, y: Int, z: Int): TileEntity? = if (isCenter(x, y, z)) tileEntity else null

    override fun getBlockMetadata(x: Int, y: Int, z: Int): Int = if (isCenter(x, y, z)) meta else 0

    override fun isAirBlock(x: Int, y: Int, z: Int): Boolean = !isCenter(x, y, z) || block.isAir(this, x, y, z)

    override fun blockExists(x: Int, y: Int, z: Int): Boolean = y in 0..255

    override fun checkChunksExist(x1: Int, y1: Int, z1: Int, x2: Int, y2: Int, z2: Int): Boolean = true

    override fun doChunksNearChunkExist(x: Int, y: Int, z: Int, radius: Int): Boolean = true

    override fun getBiomeGenForCoords(x: Int, z: Int): BiomeGenBase = biome

    override fun getLightBrightnessForSkyBlocks(x: Int, y: Int, z: Int, minBlockLight: Int): Int = 15 shl 20 or (15 shl 4)

    override fun getLightBrightness(x: Int, y: Int, z: Int): Float = 1f

    override fun getFullBlockLightValue(x: Int, y: Int, z: Int): Int = 15

    override fun getBlockLightValue(x: Int, y: Int, z: Int): Int = 15

    override fun getSavedLightValue(type: EnumSkyBlock, x: Int, y: Int, z: Int): Int = if (type == EnumSkyBlock.Sky) 15 else 0

    override fun getSkyBlockTypeBrightness(type: EnumSkyBlock, x: Int, y: Int, z: Int): Int =
        if (type == EnumSkyBlock.Sky) 15 else 0

    override fun canBlockSeeTheSky(x: Int, y: Int, z: Int): Boolean = true

    override fun getHeightValue(x: Int, z: Int): Int = if (x == X && z == Z) Y + 1 else 0

    override fun isBlockProvidingPowerTo(x: Int, y: Int, z: Int, direction: Int): Int = 0

    override fun getIndirectPowerLevelTo(x: Int, y: Int, z: Int, direction: Int): Int = 0

    override fun isSideSolid(x: Int, y: Int, z: Int, side: ForgeDirection, default: Boolean): Boolean =
        if (isCenter(x, y, z)) block.isSideSolid(this, x, y, z, side) else false

    override fun getHeight(): Int = 256

    override fun extendedLevelsInChunkCache(): Boolean = false

    companion object {
        const val X = 0
        const val Y = 64
        const val Z = 0

        private val SETTINGS = WorldSettings(0L, WorldSettings.GameType.CREATIVE, false, false, WorldType.FLAT)

        fun create(): FakeWorld = FakeWorld(WorldProvider.getProviderForDimension(0))
    }
}
