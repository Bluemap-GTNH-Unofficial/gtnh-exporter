import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.dsl.KotlinVersion

plugins {
    id("com.gtnewhorizons.gtnhconvention")
    // Version comes from the settings classpath (Kotlin Gradle plugin forced to 2.3.x in settings.gradle.kts).
    id("org.jetbrains.kotlin.jvm")
}

// Git-based versioning is disabled (this project lives in a subdirectory of the BlueMap repo).
val exporterVersion = "0.1.0"
extra["modVersion"] = exporterVersion
version = exporterVersion

base {
    archivesName.set("bluemap-gtnh-exporter")
}

// GTNHGradle forces kotlin.jvmToolchain(8); `enableModernJavaSyntax = modern` compiles Java with toolchain 25, so
// Kotlin uses toolchain 25 and emits Java 25 class files too. Runtime Kotlin stdlib comes from Forgelin 2.0.3-GTNH
// (2.1.10), so the compiler must not emit calls to newer stdlib APIs.
kotlin {
    jvmToolchain(25)
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_25)
        apiVersion.set(KotlinVersion.KOTLIN_2_1)
        languageVersion.set(KotlinVersion.KOTLIN_2_1)
    }
}

// The resource-key normalization N (contract C1) is compiled in verbatim from the legacy module, so the exporter,
// the world reader's block-state mapper and the BlueMap-side renderers share one implementation.
val sharedKotlin = layout.buildDirectory.dir("generated/sharedKotlin")
val syncSharedKotlin = tasks.register<Sync>("syncSharedKotlin") {
    from("../../legacy/src/main/kotlin") {
        include("de/bluecolored/bluemap/gtnh/legacy/ResourceKeys.kt")
    }
    into(sharedKotlin)
}
kotlin.sourceSets.named("main") {
    kotlin.srcDir(syncSharedKotlin)
}
