package de.bluecolored.bluemap.gtnh.exporter.oracle

import java.awt.image.BufferedImage
import java.awt.image.IndexColorModel
import java.io.File
import javax.imageio.ImageIO
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Camera and image conventions of the per-state oracle (identical on the BlueMap side, fork module `oracle`):
 * two orthographic views of the block `[0,1]^3` around its centre, [VIEW] x [VIEW] pixels each, side by side.
 */
object StateViews {
    const val VIEW = 128
    const val WIDTH = 2 * VIEW
    const val HEIGHT = VIEW
    const val PITCH = 30f

    /** Half the visible width/height in block units (`glOrtho(-1, 1, -1, 1, -DEPTH_RANGE, DEPTH_RANGE)`). */
    const val HALF_EXTENT = 1.0

    const val DEPTH_RANGE = 10.0
    val NAMES = listOf("se", "nw")
    val YAWS = floatArrayOf(45f, 225f)

    /** Camera basis `r, u, v` (rows of the view rotation) of view [index]: `v` points from the target to the camera. */
    fun basis(index: Int): Array<DoubleArray> {
        val p = Math.toRadians(YAWS[index].toDouble())
        val t = Math.toRadians(PITCH.toDouble())
        return arrayOf(
            doubleArrayOf(cos(p), 0.0, -sin(p)),
            doubleArrayOf(-sin(t) * sin(p), cos(t), -sin(t) * cos(p)),
            doubleArrayOf(cos(t) * sin(p), sin(t), cos(t) * cos(p)),
        )
    }
}

/** Visual-alpha recovery, face IDs from depth and PNG output of captured views. */
object StateImages {
    /** Face IDs: ForgeDirection / BlueMap Direction order + 1; 7 = other normal, 0 = nothing drawn. */
    const val OTHER = 7

    /** Dominant normal component needed for an axis face. */
    private const val AXIS_MIN = 0.95

    private val PALETTE_R = byteArrayOf(0, 64, 255.toByte(), 255.toByte(), 0, 0, 255.toByte(), 255.toByte())
    private val PALETTE_G = byteArrayOf(0, 64, 255.toByte(), 0, 255.toByte(), 0, 255.toByte(), 0)
    private val PALETTE_B = byteArrayOf(0, 64, 255.toByte(), 0, 255.toByte(), 255.toByte(), 0, 255.toByte())
    private val PALETTE_A = byteArrayOf(0, -1, -1, -1, -1, -1, -1, -1)
    private val FACE_COLORS = IndexColorModel(8, 8, PALETTE_R, PALETTE_G, PALETTE_B, PALETTE_A)

    /**
     * Straight-alpha ARGB from the same scene drawn over opaque black ([black]) and opaque white ([white]), both
     * `RGB` per pixel: `a = 1 - (white - black)`, `rgb = black / a`. This is the opacity the screen shows, whatever
     * blend functions and render passes produced it.
     */
    fun recoverAlpha(black: IntArray, white: IntArray): IntArray = IntArray(black.size) { i ->
        val b = black[i]
        val w = white[i]
        val br = b shr 16 and 0xFF
        val bg = b shr 8 and 0xFF
        val bb = b and 0xFF
        val diff = ((w shr 16 and 0xFF) - br) + ((w shr 8 and 0xFF) - bg) + ((w and 0xFF) - bb)
        val alpha = (255 - diff / 3.0).coerceIn(0.0, 255.0)
        if (alpha < 0.5) {
            0
        } else {
            val a = alpha / 255.0
            val r = (br / a).roundToInt().coerceAtMost(255)
            val g = (bg / a).roundToInt().coerceAtMost(255)
            val bl = (bb / a).roundToInt().coerceAtMost(255)
            (alpha.roundToInt() shl 24) or (r shl 16) or (g shl 8) or bl
        }
    }

    /**
     * Face IDs of one [StateViews.VIEW]-sized view from its depth buffer ([depth] in `[0, 1]`, row 0 = top,
     * 1 = nothing drawn). Orthographic depth is linear, so each pixel's surface plane follows from depth
     * differences to its neighbours: per axis the one-sided difference whose continuation (second difference over
     * the next pixel) is flat, so pixels next to a crease or an occlusion edge use their own face. Pixels with
     * colour ([argb] alpha >= 128) but no depth (geometry drawn without depth writes) get [OTHER].
     */
    fun facesFromDepth(depth: FloatArray, argb: IntArray, view: Int, width: Int, x0: Int, out: ByteArray) {
        val n = StateViews.VIEW
        val (r, u, v) = StateViews.basis(view)
        val pixel = 2 * StateViews.HALF_EXTENT / n
        fun covered(i: Int, j: Int) = i in 0 until n && j in 0 until n && depth[j * width + x0 + i] < 1f
        // distance from the camera plane in block units (glOrtho: z_eye = R (1 - 2 d))
        fun z(i: Int, j: Int) = StateViews.DEPTH_RANGE * (2.0 * depth[j * width + x0 + i] - 1.0)

        fun slope(i: Int, j: Int, di: Int, dj: Int): Double {
            val z0 = z(i, j)
            val back = covered(i - di, j - dj)
            val front = covered(i + di, j + dj)
            if (!back && !front) return Double.NaN
            val zb = if (back) z(i - di, j - dj) else 0.0
            val zf = if (front) z(i + di, j + dj) else 0.0
            if (back && front) {
                val rb = if (covered(i - 2 * di, j - 2 * dj)) abs(z0 - 2 * zb + z(i - 2 * di, j - 2 * dj)) else Double.MAX_VALUE / 4
                val rf = if (covered(i + 2 * di, j + 2 * dj)) abs(z(i + 2 * di, j + 2 * dj) - 2 * zf + z0) else Double.MAX_VALUE / 4
                return if (rb <= rf) z0 - zb else zf - z0
            }
            return if (back) z0 - zb else zf - z0
        }

        for (j in 0 until n) {
            for (i in 0 until n) {
                val index = j * width + x0 + i
                if (!covered(i, j)) {
                    out[index] = if (argb[index] ushr 24 >= 128) OTHER.toByte() else 0
                    continue
                }
                val dx = slope(i, j, 1, 0)
                val dy = slope(i, j, 0, -1) // screen y grows upwards, rows downwards
                if (dx.isNaN() || dy.isNaN()) {
                    out[index] = OTHER.toByte()
                    continue
                }
                val a = dx / pixel
                val b = dy / pixel
                // surface point (q - c) = sx r + sy u - z v  =>  normal ~ a r + b u + v
                val nx = a * r[0] + b * u[0] + v[0]
                val ny = a * r[1] + b * u[1] + v[1]
                val nz = a * r[2] + b * u[2] + v[2]
                out[index] = classify(nx, ny, nz).toByte()
            }
        }
    }

    /** Face ID of a (not necessarily unit) normal. */
    fun classify(nx: Double, ny: Double, nz: Double): Int {
        val length = sqrt(nx * nx + ny * ny + nz * nz)
        if (length < 1e-12) return OTHER
        val x = nx / length
        val y = ny / length
        val z = nz / length
        return when {
            y <= -AXIS_MIN -> 1
            y >= AXIS_MIN -> 2
            z <= -AXIS_MIN -> 3
            z >= AXIS_MIN -> 4
            x <= -AXIS_MIN -> 5
            x >= AXIS_MIN -> 6
            else -> OTHER
        }
    }

    fun writeArgb(argb: IntArray, file: File) {
        val image = BufferedImage(StateViews.WIDTH, StateViews.HEIGHT, BufferedImage.TYPE_INT_ARGB)
        image.setRGB(0, 0, StateViews.WIDTH, StateViews.HEIGHT, argb, 0, StateViews.WIDTH)
        file.parentFile.mkdirs()
        ImageIO.write(image, "png", file)
    }

    fun writeFaces(faces: ByteArray, file: File) {
        val image = BufferedImage(StateViews.WIDTH, StateViews.HEIGHT, BufferedImage.TYPE_BYTE_INDEXED, FACE_COLORS)
        image.raster.setDataElements(0, 0, StateViews.WIDTH, StateViews.HEIGHT, faces)
        file.parentFile.mkdirs()
        ImageIO.write(image, "png", file)
    }
}
