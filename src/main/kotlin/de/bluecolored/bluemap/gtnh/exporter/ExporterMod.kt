package de.bluecolored.bluemap.gtnh.exporter

import cpw.mods.fml.common.FMLCommonHandler
import cpw.mods.fml.common.Mod
import cpw.mods.fml.common.event.FMLInitializationEvent
import cpw.mods.fml.common.eventhandler.SubscribeEvent
import cpw.mods.fml.common.gameevent.TickEvent
import net.minecraft.client.Minecraft
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.Logger
import java.io.File

/**
 * One-shot client exporter. Once the client has finished loading (first client tick with a screen open, i.e. the
 * main menu after the resource reload), it exports the BlueMap resource pack and quits the game:
 * exit code 0 on success, 1 if the export failed.
 *
 * System properties:
 * - `bluemap.gtnh.exporter.out`: output directory (default `<game dir>/bluemap-gtnh-export`),
 * - `bluemap.gtnh.exporter.extraStates`: optional file with extra `modid:name meta` lines (e.g. from world scans),
 * - `bluemap.gtnh.exporter.stay`: `true` keeps the client running after the export (debugging).
 */
@Mod(
    modid = ExporterMod.MOD_ID,
    name = "BlueMap GTNH Exporter",
    useMetadata = true,
    acceptableRemoteVersions = "*",
    dependencies = "required-after:forgelin;required-after:gtnhlib;after:gregtech",
    modLanguageAdapter = "net.shadowfacts.forgelin.KotlinAdapter",
)
object ExporterMod {
    const val MOD_ID = "bluemapgtnhexporter"

    val log: Logger = LogManager.getLogger("BlueMapGtnhExporter")

    private var ticksWithScreen = 0
    private var done = false

    @Mod.EventHandler
    fun init(event: FMLInitializationEvent) {
        if (!event.side.isClient) return
        FMLCommonHandler.instance().bus().register(this)
    }

    @SubscribeEvent
    fun onClientTick(event: TickEvent.ClientTickEvent) {
        if (event.phase != TickEvent.Phase.END || done) return
        val mc = Minecraft.getMinecraft()
        if (mc.currentScreen == null) return
        // a few ticks of slack so that late resource reload listeners have run
        if (++ticksWithScreen < 20) return
        done = true

        val out = File(System.getProperty("bluemap.gtnh.exporter.out") ?: File(mc.mcDataDir, "bluemap-gtnh-export").path)
        val extraStates = System.getProperty("bluemap.gtnh.exporter.extraStates")?.let(::File)
        val ok = try {
            Exporter(mc, out.absoluteFile, extraStates).run()
            true
        } catch (t: Throwable) {
            log.fatal("BlueMap GTNH export failed", t)
            false
        }
        if (System.getProperty("bluemap.gtnh.exporter.stay") == "true") return
        if (ok) {
            log.info("Export finished, shutting down the client")
            mc.shutdown()
        } else {
            FMLCommonHandler.instance().exitJava(1, false)
        }
    }
}
