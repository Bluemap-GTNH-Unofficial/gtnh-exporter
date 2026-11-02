package de.bluecolored.bluemap.gtnh.exporter

import cpw.mods.fml.common.FMLCommonHandler
import cpw.mods.fml.common.Mod
import cpw.mods.fml.common.event.FMLInitializationEvent
import cpw.mods.fml.common.eventhandler.SubscribeEvent
import cpw.mods.fml.common.gameevent.TickEvent
import de.bluecolored.bluemap.gtnh.exporter.oracle.CameraCapture
import de.bluecolored.bluemap.gtnh.exporter.oracle.StateCapture
import net.minecraft.client.Minecraft
import org.apache.logging.log4j.LogManager
import org.apache.logging.log4j.Logger
import java.io.File

/**
 * Client mod with three modes (system property `bluemap.gtnh.exporter.mode`):
 *
 * - `export` (default): once the client has finished loading (first client tick with a screen open, i.e. the main
 *   menu after the resource reload), exports the BlueMap resource pack and quits the game,
 * - `states`: at the same point, renders every listed block state in isolation for the per-state rendering oracle
 *   ([StateCapture]) and quits,
 * - `cameras`: joins a scene server and captures camera views for the world-scene oracle ([CameraCapture]).
 *
 * One-shot modes exit with code 0 on success, 1 if they threw.
 *
 * System properties:
 * - `bluemap.gtnh.exporter.out`: output directory (default `<game dir>/bluemap-gtnh-export`),
 * - `bluemap.gtnh.exporter.extraStates`: optional file with extra `modid:name meta` lines (e.g. from world scans),
 * - `bluemap.gtnh.exporter.states`: `states` mode: the file of states to render (see [StateCapture]),
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
    const val MODE_PROPERTY = "bluemap.gtnh.exporter.mode"

    val log: Logger = LogManager.getLogger("BlueMapGtnhExporter")

    private var mode = "export"
    private var ticksWithScreen = 0
    private var done = false

    @Mod.EventHandler
    fun init(event: FMLInitializationEvent) {
        if (!event.side.isClient) return
        mode = System.getProperty(MODE_PROPERTY)?.takeIf { it.isNotBlank() } ?: "export"
        when (mode) {
            "export", "states" -> FMLCommonHandler.instance().bus().register(this)
            "cameras" -> CameraCapture.start()
            else -> log.error("Unknown {} '{}': doing nothing", MODE_PROPERTY, mode)
        }
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
        val ok = try {
            if (mode == "states") {
                val states = System.getProperty("bluemap.gtnh.exporter.states")?.let(::File)
                    ?: error("states mode needs -Dbluemap.gtnh.exporter.states=<file>")
                StateCapture(mc, out.absoluteFile, states).run()
            } else {
                val extraStates = System.getProperty("bluemap.gtnh.exporter.extraStates")?.let(::File)
                Exporter(mc, out.absoluteFile, extraStates).run()
            }
            true
        } catch (t: Throwable) {
            log.fatal("BlueMap GTNH {} failed", mode, t)
            false
        }
        if (System.getProperty("bluemap.gtnh.exporter.stay") == "true") return
        if (ok) {
            log.info("{} finished, shutting down the client", mode)
            mc.shutdown()
        } else {
            FMLCommonHandler.instance().exitJava(1, false)
        }
    }
}
