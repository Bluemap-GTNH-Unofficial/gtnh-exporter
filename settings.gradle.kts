pluginManagement {
    repositories {
        maven {
            // RetroFuturaGradle / GTNHGradle
            name = "GTNH Maven"
            url = uri("https://nexus.gtnewhorizons.com/repository/public/")
            mavenContent {
                includeGroup("com.gtnewhorizons")
                includeGroupByRegex("com\\.gtnewhorizons\\..+")
            }
        }
        gradlePluginPortal()
        mavenCentral()
        mavenLocal()
    }
}

// GTNHGradle puts Kotlin Gradle plugin 2.2.21 on the settings classpath; its highest JvmTarget is 24.
// Kotlin 2.3 can emit Java 25 class files, matching `enableModernJavaSyntax = modern` (Java toolchain 25).
buildscript {
    repositories {
        mavenCentral()
    }
    dependencies {
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:2.3.21")
    }
}

plugins {
    id("com.gtnewhorizons.gtnhsettingsconvention") version("2.0.33")
}

rootProject.name = "bluemap-gtnh-exporter"
