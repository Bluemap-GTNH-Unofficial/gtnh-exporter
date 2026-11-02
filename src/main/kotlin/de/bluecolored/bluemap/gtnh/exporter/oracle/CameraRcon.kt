package de.bluecolored.bluemap.gtnh.exporter.oracle

import java.io.Closeable
import java.io.DataInputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Minimal RCON client for the camera mode, with the 1.7.10 quirks the orchestrator's client handles too: one request
 * in flight at a time (the server reads a request with a single `read()`), Latin-1 replies, replies longer than 4096
 * characters split into fragments without an end marker.
 */
internal class CameraRcon(host: String, port: Int, password: String) : Closeable {
    private val socket = Socket()
    private val input: DataInputStream
    private var nextId = 1

    init {
        socket.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
        socket.soTimeout = READ_TIMEOUT_MS
        input = DataInputStream(socket.getInputStream())
        val id = send(TYPE_LOGIN, password)
        val (responseId, _) = read()
        if (responseId == -1 || responseId != id) {
            close()
            throw IOException("RCON authentication failed")
        }
    }

    fun command(command: String): String {
        val id = send(TYPE_COMMAND, command)
        val (responseId, first) = read()
        if (responseId != id) throw IOException("unexpected RCON response id $responseId, expected $id")
        val reply = StringBuilder(first)
        var fragment = first
        while (fragment.length >= FRAGMENT_SIZE) {
            socket.soTimeout = CONTINUATION_TIMEOUT_MS
            fragment = try {
                read().second
            } catch (_: SocketTimeoutException) {
                break
            } finally {
                socket.soTimeout = READ_TIMEOUT_MS
            }
            reply.append(fragment)
        }
        return reply.toString()
    }

    private fun send(type: Int, body: String): Int {
        val id = nextId++
        val bytes = body.toByteArray(Charsets.ISO_8859_1)
        val packet = ByteBuffer.allocate(4 + 8 + bytes.size + 2).order(ByteOrder.LITTLE_ENDIAN)
        packet.putInt(8 + bytes.size + 2).putInt(id).putInt(type).put(bytes).put(0).put(0)
        socket.getOutputStream().apply {
            write(packet.array())
            flush()
        }
        return id
    }

    private fun read(): Pair<Int, String> {
        val length = Integer.reverseBytes(input.readInt())
        val data = ByteArray(length)
        input.readFully(data)
        val header = ByteBuffer.wrap(data, 0, 8).order(ByteOrder.LITTLE_ENDIAN)
        val id = header.int
        return id to String(data, 8, maxOf(0, length - 10), Charsets.ISO_8859_1)
    }

    override fun close() {
        socket.close()
    }

    private companion object {
        const val TYPE_COMMAND = 2
        const val TYPE_LOGIN = 3
        const val FRAGMENT_SIZE = 4096
        const val CONNECT_TIMEOUT_MS = 10_000
        const val READ_TIMEOUT_MS = 60_000
        const val CONTINUATION_TIMEOUT_MS = 300
    }
}
