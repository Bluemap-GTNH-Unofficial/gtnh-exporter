package de.bluecolored.bluemap.gtnh.exporter

import net.minecraft.block.Block
import net.minecraft.client.renderer.texture.TextureAtlasSprite
import net.minecraft.util.IIcon
import java.lang.reflect.Method

/**
 * Angelica's MCPatcher connected textures of `method=compact`: five tiles (none, all, up/down, left/right and all but
 * the corners connected), e.g. GT5-Unofficial's `assets/bartworks/mcpatcher/ctm/bwtieredglass/<glass>/`. Read from
 * the overrides Angelica loaded, by reflection (Angelica is not a compile dependency). A face of a matched block is
 * tile 0 alone and, next to matching blocks, four quarters picked like MyCTMLib's `compact` layout
 * ([TextureExporter.recordMcPatcherCompact]). Icons that MyCTMLib connects itself are left to MyCTMLib, here as in the
 * client (MyCTMLib's `CTMUtils.hasCandidates` mixin).
 */
class McPatcherCtm private constructor(
    private val getBlockIcon: Method,
    /** Tile 0 of every loaded compact override -> its five tiles. */
    private val compact: Map<IIcon, List<TextureAtlasSprite>>,
) {
    val size: Int get() = compact.size

    /** The five tiles when Angelica replaces [icon] of [block] with meta [meta] on [side] by a compact override's tile 0. */
    fun tiles(
        icon: IIcon,
        block: Block,
        side: Int,
        meta: Int,
    ): List<TextureAtlasSprite>? {
        val held = try {
            getBlockIcon.invoke(null, icon, block, side, meta) as? IIcon
        } catch (_: Throwable) {
            null
        } ?: return null
        return if (held === icon) null else compact[held]
    }

    /** The five tiles when [icon] is a compact override's tile 0, as Angelica draws a face without matching neighbours. */
    fun tilesOf(icon: IIcon): List<TextureAtlasSprite>? = compact[icon]

    companion object {
        /** The loaded compact overrides, or null without Angelica's MCPatcher CTM. */
        fun load(): McPatcherCtm? {
            val utils = try {
                Class.forName("com.prupe.mcpatcher.ctm.CTMUtils")
            } catch (_: Throwable) {
                return null
            }
            return try {
                val getBlockIcon = utils.getMethod(
                    "getBlockIcon",
                    IIcon::class.java,
                    Block::class.java,
                    Int::class.javaPrimitiveType,
                    Int::class.javaPrimitiveType,
                )
                val overrides = utils.getDeclaredField("overrides").apply { isAccessible = true }.get(null)
                val all = overrides.javaClass.getMethod("getAll").invoke(overrides) as List<*>
                val icons = Class.forName("com.prupe.mcpatcher.ctm.TileOverride").getDeclaredField("icons")
                    .apply { isAccessible = true }
                val compactClass = Class.forName("com.prupe.mcpatcher.ctm.TileOverrideImpl\$CTMCompact")
                val compact = HashMap<IIcon, List<TextureAtlasSprite>>()
                for (override in all) {
                    if (!compactClass.isInstance(override)) continue
                    val tiles = (icons.get(override) as? Array<*>)?.map { it as? TextureAtlasSprite } ?: continue
                    if (tiles.size != 5 || tiles.any { it == null }) continue
                    compact.putIfAbsent(tiles[0]!!, tiles.map { it!! })
                }
                ExporterMod.log.info("MCPatcher CTM: {} compact overrides", compact.size)
                McPatcherCtm(getBlockIcon, compact)
            } catch (t: Throwable) {
                ExporterMod.log.warn("Reading Angelica's MCPatcher CTM overrides failed: {}", t.toString())
                null
            }
        }
    }
}
