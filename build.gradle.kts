import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.dsl.KotlinVersion

plugins {
    id("com.gtnewhorizons.gtnhconvention")
    // Version comes from the settings classpath (Kotlin Gradle plugin forced to 2.3.x in settings.gradle.kts).
    id("org.jetbrains.kotlin.jvm")
}

// Fixed version instead of GTNHGradle's git-based versioning.
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

// The resource-key normalization N (contract C1) is compiled in verbatim from the BlueMap fork's legacy module, so the
// exporter, the world reader's block-state mapper and the BlueMap-side renderers share one implementation. The fork is
// a sibling checkout: <workspace>/BlueMap next to <workspace>/gtnh-exporter (workspace override: BMGTNH_WORKSPACE).
val workspaceDir = providers.environmentVariable("BMGTNH_WORKSPACE").map { file(it) }.getOrElse(rootDir.parentFile)
val sharedKotlinSource = workspaceDir.resolve("BlueMap/legacy/src/main/kotlin")
val resourceKeysPath = "de/bluecolored/bluemap/gtnh/legacy/ResourceKeys.kt"
val sharedKotlin = layout.buildDirectory.dir("generated/sharedKotlin")
val syncSharedKotlin = tasks.register<Sync>("syncSharedKotlin") {
    // A provider, so the check runs only when the task is scheduled (an empty source would just skip the task).
    from(
        provider {
            if (!sharedKotlinSource.resolve(resourceKeysPath).isFile) {
                throw GradleException(
                    "$resourceKeysPath not found under $sharedKotlinSource: clone " +
                        "https://github.com/Bluemap-GTNH-Unofficial/BlueMap next to gtnh-exporter (or set BMGTNH_WORKSPACE)",
                )
            }
            sharedKotlinSource
        },
    ) {
        include(resourceKeysPath)
    }
    into(sharedKotlin)
}
kotlin.sourceSets.named("main") {
    kotlin.srcDir(syncSharedKotlin)
}
