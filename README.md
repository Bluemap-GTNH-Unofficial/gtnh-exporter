# bluemap-gtnh-exporter

One-shot **client** mod (Forge 1.7.10, Kotlin, GTNHGradle `modern` = Java 25 bytecode, package
`de.bluecolored.bluemap.gtnh.exporter`, modid `bluemapgtnhexporter`) for GT New Horizons 2.9.0-RC-2. Once the
client has loaded (20 client ticks after the first screen opens, i.e. the main menu), it exports the appearance of
every registered non-`minecraft` block into a BlueMap resource pack, then quits the game (exit 0; exit 1 if the
export threw). Vanilla blocks are rendered from the 1.13 client jar (Phase 2) and are not exported.

The BlueMap side (renderer `gtnh:quads`, the quad-model loader and the coverage report) is the main-build module
`gtnh-render` (`./gradlew :gtnh-render:test :gtnh-render:addonJar`).

## One command

```sh
cd BlueMap-GTNH/tools/gtnh-exporter/launcher
uv run gtnh-client export --clean --extra-states-dump ../../../../out/scenes/gtnh-zoo/dump.ndjson
```

builds this mod with Gradle, unpacks a fresh client into `WS/clients/gtnh-2.9.0-RC-2` from the Prism zip, fetches
the Mojang/Forge/LWJGL3 libraries and assets (cache `WS/downloads/mc-cache`), starts the client minimized with plain
`java` (see `launcher/README.md`), and checks the outputs in `WS/out/exporter/`:

- `bluemap-gtnh-resources-2.9.0-RC-2.zip` — drop into BlueMap's `packs/` (with the `gtnh-render` and `legacy` addons),
- `export-report.json` — per-mod counts (also inside the zip as `gtnh/export-report.json`).

Build alone: `mise exec java@temurin-25.0.5+8.0.LTS -- ./gradlew -Porg.gradle.java.installations.paths=$(mise where java@zulu-8.98.0.107)/Contents/Home,$(mise where java@temurin-25.0.5+8.0.LTS)/Contents/Home build`
(Kotlin Gradle plugin forced to 2.3.21 in `settings.gradle.kts`, D11). The C1 normalization
`legacy/.../ResourceKeys.kt` is compiled in verbatim (`syncSharedKotlin`), so mapper and exporter cannot diverge.

System properties (the launcher sets them): `bluemap.gtnh.exporter.out` (output dir), `bluemap.gtnh.exporter.extraStates`
(file of `<modid:name> <meta>` lines, e.g. from world scans; the meta is the last token), `bluemap.gtnh.exporter.stay=true`
(keep the client open), `bluemap.gtnh.exporter.skipBlocks=true` (GregTech dump only).

## What is exported

For every block × meta (metas = 0..15, `getSubBlocks` item damages / `Item.getMetadata`, extra states; probed metas
0..15 that yield nothing are dropped as invalid), evaluated in `FakeWorld`, a real client `World` with only that block
(and its `createTileEntity` tile entity, `worldObj` set) at (0, 64, 0), air around, full light, plains biome:

| Case | Output |
|---|---|
| render type 0 and full bounds | JSON cube `assets/<ns>/models/block/<name>/<n>.json`, textures from `getIcon(world, x, y, z, side)`, `cullface` on every face, `tintindex 0` if tinted, `light_emission` = light value |
| any other render type (vanilla shapes, ISBRHs) | quads captured with GTNHLib `TessellatorManager` around `RenderBlocks.renderBlockByRenderType` (`renderAllFaces`, AO off, fancy graphics, Forge world render pass 0 and 1 as `canRenderInPass` allows) → `assets/<ns>/models/gtnh_quads/<name>/<n>.json`, variant `"renderer": "gtnh:quads"` |
| tile-entity special renderer only, render type −1, or a renderer that draws nothing | static fallback: bounding-box cuboid `models/block/<name>/bbox_<n>.json` with the block's own icons (plan risk table: "包围盒或静态回退"); reported as `bbox` with the original reason in `blocks.ndjson` (`fallbackFor`) |
| no icon / exception | no variant; reason in the report |

Identical models of one block are shared. Blockstates `assets/<N(modid)>/blockstates/<N(name)>.json` have one variant
`meta=<n>` per exported meta (C1 normalization; meta 0 first, which BlueMap also uses for metas without a variant).

**Quad model format** (parsed by `gtnh-render` `QuadModel`, also a valid vanilla model so its `textures` enter the atlas):
`{"textures": {"t0": "<ns>:block/<path>"}, "gtnh_quads": [{"texture": "#t0", "vertices": [[x,y,z,u,v] ×4], "color": "#rrggbb", "tint": true, "cullface": "up", "light_emission": 7}]}`
— positions in block pixels (0..16, may exceed), uv in sprite pixels (0..16), tessellator vertex order (counter-clockwise
from the front). Vertex colors are averaged per quad and divided by vanilla's non-AO face shade (0.5 down, 0.8 z, 0.6 x);
quads whose color equals the biome tint at capture become `"tint": true` (white). Atlas UVs are mapped to the sprite
containing the quad's UV centroid (quads outside every sprite are dropped, counted as `unmappedQuads`). `cullface` is set
for quads lying on a block face and facing out.

**Textures** `assets/<ns>/textures/block/<N(path segments)>.png`, `ns` = N(icon domain), vanilla icons under
`gtnh_minecraft_1_7` (not to clash with 1.13 textures). Source: the PNG through the resource manager (`.png.mcmeta`
copied; strips without one are cut to the first frame), else the sprite's frame data, else a crop of the GL atlas.

**Colors** `assets/<ns>/blockColors.json`: `colorMultiplier` probed in 7 vanilla biomes; equal to the biome's grass /
foliage / water color everywhere → `@grass` / `@foliage` / `@water`; constant non-white → `#rrggbb`; biome-dependent
but unmatched → the plains value (reported as `biome-unmatched`).

**Properties** `assets/<ns>/blockProperties.json`: `culling` = `occluding` = `isOpaqueCube`; `cullingIdentical` for
`BlockBreakable` (glass-like).

**Shapes** `data/gtnh/block_shapes.json` (for the legacy reader's neighbour fixers, agreed with VanillaFlattening):
`{"format": 1, "blocks": {"<ns>:<name>": {"*": shape, "<meta>": shape}}}` with shape `{"fullCube": renderAsNormalBlock,
"opaque": material.isOpaque && isOpaqueCube, "solidSides": bitmask of isSideSolid by ForgeDirection ordinal,
"fenceConnect": Blocks.fence.canConnectFenceTo(block), "paneConnect": iron_bars.canPaneConnectToBlock(block)}`;
`*` is meta 0, only differing metas are listed.

**Diagnostics** `gtnh/blocks.ndjson`: one line per block — class, render type, opacity, pass, light, and per meta:
bounds, icons per side, render color, tint, tile entity class, result (`cube`/`quads`/`bbox`/`failed`), model, reason.

## Coverage and screenshots

```sh
uv run gtnh-client coverage      # builds :cli:shadowJar :legacy:addonJar :gtnh-render:addonJar, renders WS/worlds/gtnh-zoo
uv run gtnh-client screens       # serves that render on 127.0.0.1:8210, Playwright/Chromium screenshots -> WS/out/exporter/screens
```

`coverage` writes a CLI config to `WS/out/exporter/coverage/config` (map with `loader: "gtnh:anvil_1_7"`, render mask =
the scene's dump box, packs = both addons + the zip), force-renders with `-Dbluemap.gtnh.coverageReport=<file>`
(`gtnh-render` `CoverageReport`: every distinct state BlueMap computes properties for is classified `ok` / `fallback` (no
matching variant, first variant drawn) / `no_blockstate` / `no_variant` / `missing_model`), then compares with the scene's
`dump.ndjson`: per mod, distinct non-vanilla placed `(name, meta)` and how many rendered with their own model, plus the
exporter result of each → `coverage/coverage.json`.

## GregTech dump (gtnh/gregtech/)

`gt/GtDump.kt` runs on the client thread after the block export, once resources are loaded. It writes four
compact JSON files into the pack. Its summary goes into `export-report.json` under `"gregtech"`: per-step
`<step>Seconds`, `<step>Error` if a step threw, `mte.{total,ok,failed,kinds,failedReasons,unknownTextureClasses}`,
`casings`, `casingErrors`, `materials`, `oreBlocks`, `errors` and `seconds`.

Every icon is recorded twice. `icon` is the raw 1.7.10 icon name (`domain:path`; vanilla icons have no domain).
`key` is the BlueMap texture key that `TextureExporter` returned (`<ns>:block/<path>`, file
`assets/<ns>/textures/block/<path>.png` in the pack). `key` is null when the icon could not be exported and for
GT's invisible icon (`icon: "invisible"`). Sides and facings always use ForgeDirection ordinals:
0 down, 1 up, 2 north, 3 south, 4 west, 5 east. `rgba` is GT's `short[4]`. Only R, G and B are used for tinting;
GT ignores alpha (usually 0).

### Layers and faces

A *face* is the list of layers GT renders on one block side, bottom layer first. Each file has its own
`layers` table, and faces refer to layers by index. The faces are already resolved:
`GTMultiTextureRender` is flattened in order, `GTSidedTextureRender` is replaced by its texture for that side, and
a copied block is resolved to its icon for that side. Layer objects (`type`):

- `"icon"` (`GTRenderedTexture` and subclasses):
  `{type, class?, invisible?, icon, key, overlay?, overlayKey?, pass?, rgba, glow, extFacing?}`.
  - The base `icon` is tinted with `rgba`. It is skipped when `invisible` is true, but the overlay is still drawn.
  - `overlay` is drawn on top, untinted.
  - `glow`: fullbright, no AO. GT skips glow layers if `render.renderGlowTextures` is false.
  - `pass` (0 if absent): the only render pass the layer is drawn in. 1 means alpha-blended.
  - `extFacing`: UVs are rotated and flipped by the MTE's ExtendedFacing; see "UV orientation".
  - `class`: present for subclasses, e.g. TecTech's `TTRenderedExtendedFacingTexture`.
- `"block"` (`GTCopiedBlockTextureRender`; with `ctm: true` for `GTCopiedCTMBlockTexture`):
  `{type, ctm?, block, meta, copiedSide, iconError?, icon, key, pass?}`.
  - The icon of block registry name `block` at `meta`, for side `copiedSide`, or for the rendered side if
    `copiedSide` is 6. It is drawn untinted.
  - `pass` is the block's render pass.
  - `iconError`: `Block.getIcon` threw. The casing points at an unused meta, so GT would fail to render it too.
- `"background"`: placeholder for a hatch's casing background, used only in `mte.json` `hatch.front`.
- `"unknown"`: `{type, class, icon?, key?, rgba?}`, for an ITexture implementation without a descriptor.
  None occurs in 2.9.0-RC-2.

UV orientation (`GTRenderedTexture`, world rendering):
- Faces use vanilla `RenderBlocks.renderFace*` with `field_152631_f = true` on the north and east faces, i.e. the
  usual 1.7.10 mirroring is undone.
- The down face is additionally U-flipped unless `render.fixedBottomFaceUV` is true (hodgepodge `fixBottomFaceUV`).
- `extFacing` layers on a TE with an alignment (`alignment: true`) are rotated with `uvRotate*` and flipped per
  `GTRenderedTexture.getFlipped` from the TE's ExtendedFacing (NBT `eRotation` / `eFlip`).
- `extFacing` layers on a TE without an alignment have east and down U-flipped and north V-flipped.
- Copied-block layers set `field_152631_f` on east and north only.

### `mte.json`

```
{
  "render":  { "fixedBottomFaceUV": bool, "renderGlowTextures": bool },
  "layers":  [ Layer, ... ],
  "faces":   [ [layerIndex, ...], ... ],          // interned faces
  "classes": { "<class>": "<superclass>" | null }, // superclass chain of every entry class (stops at Object)
  "entries": [ Entry, ... ],                       // one per non-null GregTechAPI.METATILEENTITIES[id], id order
  "errors":  [ { "id", "error", "at" } ]           // MTEs whose texture dump threw (error is also in the entry)
}
Entry = {
  "id": int, "name": mName, "unlocalizedName": "gt.blockmachines.<mName>.name", "class": "<fqcn>",
  "kind": "cable" | "fluid_pipe" | "item_pipe" | "frame" | "pipe" | "hatch" | "multiblock_controller" |
          "basic_machine" | "generator" | "transformer" | "battery_buffer" | "hull" | "tank" |
          "tiered_machine" | "machine",
  "baseType": int,          // getTileEntityBaseType() = block meta of gt.blockmachines (4..11: BaseMetaPipeEntity)
  "tier"?: int,             // MTETieredMachineBlock.mTier
  "alignment"?: true,       // IAlignmentProvider (multiblock controllers: ExtendedFacing from NBT eRotation/eFlip)
  "tesr"?: true,            // IMTERenderer: also has a TESR, not represented here
  "customRender"?: "<fqcn>",// class overriding renderInWorld (custom ISBR geometry), not represented here
  "error"?: "<reason>",
  // non-pipes:
  "textures": [facing 0..5][state 0 = inactive, 1 = active][side 0..5] -> face index,
  "outputFace"?: [state] -> { "down": face, "up": face, "side": face },   // basic machines only
  "hatch"?: { "front": [inactive face, active face], "defaultBackground": [side 0..5] -> face },
  // MetaPipeEntity (cables, pipes, frames):
  "pipe"?: { "thickness": float, "collisionThickness"?: float,
             "material"?: { "name", "id", "rgba", "iconSet" } | { "name", "class", "rgba"? },
             "insulated"?: bool, "fluidChannels"?: int,
             "connected": face, "unconnected": face }
}
```

How the values were produced:
- Each MTE is built the way GT places one: `GregTechAPI.createTileEntity(baseType)`, then
  `setInitialValuesAsNBT(null, id)`.
- `textures[f][s][side]` is `mte.getTexture(base, side, f, colorIndex = -1, active, redstone = false)`. Before the
  call the base TE's `mFacing` is set to `f` and `mActive` to `s`; basic machines also get `mMainFacing = f`. These
  are uncoloured, uncovered faces.
- Basic machines have a separate output facing (`mFacing`, while the front is `mMainFacing`). In `outputFace`,
  `down`, `up` and `side` are the faces of the output side when the main facing is north and the output faces down,
  up or south. GT does not distinguish between horizontal output sides.
- `hatch.front` holds `getTexturesInactive/Active(background)`. Its `background` layer stands for the hatch's casing
  texture on that side:
  - if NBT `texturePage`/`textureIndex` is non-zero: `casings.json` `casings[page << 7 | index]`,
  - otherwise `MACHINE_CASINGS[tier][colour + 1]`. Uncoloured, that is `defaultBackground`.
  The non-front sides show only the background.
- Pipes are a straight west–east pipe, as in the GT inventory renderer:
  - `connected` = `getTexture(base, WEST, W|E, -1, true, false)`, the end face, also used when the pipe has no
    connections,
  - `unconnected` = `getTexture(base, DOWN, W|E, -1, false, false)`, the side faces.

  Geometry follows `MetaPipeEntity.renderInWorld`: a `thickness`-wide core plus arms towards connected sides
  (NBT `mConnections`, bit `1 << side`). Thickness ≥ 1 renders a full cube.

### `casings.json`

```
{
  "errorTextureId": 225,
  "layers": [ Layer, ... ],
  "casings": { "<id = page << 7 | index>": [side 0..5] -> [layerIndex, ...] },  // every non-null casingTexturePages entry
  "machineCasings": [tier 0..14][colour 0..16][side 0..5] -> [layerIndex, ...],    // Textures.BlockIcons.MACHINE_CASINGS
  "errors": [ { "id", "error" } ]
}
```

`machineCasings` colour 0 is uncoloured (`Dyes.MACHINE_METAL`); colour `c + 1` is painted with dye `c`.

### `materials.json`

```
{
  "materials": { "<id>": Material },  // GregTechAPI.sGeneratedMaterials[id] (ore meta / frame / pipe material ids)
  "unindexed": [ Material ],          // other Materials.getAll() entries (id -1 or a non-canonical duplicate)
  "iconSets": { "<setKey>": { "name": mSetName, "custom"?: true,
                              "blockIcons": { "<MaterialIconRegistry.IconType>": IconRef } } },
  "dyes": [ { "index", "name": "dyeBlack", "colorName": "Black", "rgba" } ],  // Dyes.VALUES
  "machineMetal": rgba
}
Material = { "id": mMetaItemSubID, "name": mName, "localName": mDefaultLocalName, "rgba": mRGBa,
             "iconSet": "<setKey>", "glowingOre"?: true }
IconRef  = { "icon", "key", "overlay"?, "overlayKey"?, "pass"? }   // IIconContainer.getIcon / getOverlayIcon / getRenderIconPass
```

- `setKey` is the TextureSet's `mSetName`. A different instance with the same name gets `#2`, `#3`, … appended.
- `blockIcons` lists the non-void BLOCK and BLOCK_WITH_ALPHA icon types, e.g. `ORE`, `ORE_SMALL`, `BLOCK1`..`BLOCK6`,
  `PIPE_*`, `WIRE`, `FRAME_GT` and `SHEETMETAL`.
- Painted pipes and cables use `Dyes.getModulation(colour, material.rgba)`: the dye's `rgba` if `0 <= colour < 16`,
  otherwise the material's.

### `ores.json`

```
{
  "metaEncoding": { "materialModulo": 1000, "stoneDivisor": 1000, "naturalOffset": 8000, "smallOffset": 16000 },
  "layers": [ Layer, ... ],
  "blocks": [ { "block": "gregtech:gt.blockores2",
                "stoneTypes": [ null | { "index", "name": StoneType, "icons": [side 0..5] -> { "icon", "key" },
                                         "background": [layerIndex, ...] } ],   // index 0..7
                "examples": [ { "meta", "faces": [side 0..5] -> [layerIndex, ...] } ] } ],
  "oreTextures": { "<setKey>": { "normal": IconRef, "small": IconRef,
                                 "perStone"?: { "<StoneType>": { "normal": IconRef, "small": IconRef } } } },
  "noMaterial": IconRef,
  "exampleMaterial": { "id", "name" }
}
```

Ore rendering rule (`GTBlockOre.getTextures`, the same on all six faces):
1. Decode the meta. `mat = meta % 1000`, `stone = (meta % 16000 % 8000) / 1000`, `small = meta >= 16000`. The
   natural flag (`meta % 16000 >= 8000`) does not affect textures.
2. Background layer: `stoneTypes[stone].background`. It is `StoneType.getTexture(0)`, so the stone's side-0 icon
   is used on every face. If the slot is null, use StoneType `Stone` (`gt.blockores2` index 0).
3. Foreground layer: `oreTextures[materials[mat].iconSet]`, taking `perStone[stoneName]` if present, then
   `small` or `normal`. Its icon is tinted with the material's `rgba`, the `overlay` is untinted on top, glow is
   set if `glowingOre`, and pass is 1. If `materials[mat]` does not exist, use `noMaterial` untinted.
4. `examples` holds GT's own `getTextures(meta)` result for `exampleMaterial`, normal and small, for every
   stone slot. Use it to test an implementation of the rule.

### What Phase 4 still needs

- Read the TE NBT and pick the variant:
  - `mID`, `mFacing`, `mActive`, `mColor` (colour = `mColor - 1`),
  - basic machines: `mMainFacing`,
  - hatches: `texturePage`/`textureIndex`,
  - pipes: `mConnections`,
  - controllers: `eRotation`/`eFlip`.
- Painted machines and pipes. The dump is uncoloured (colorIndex −1). Re-tint the casing layers via
  `machineCasings[tier][colour + 1]`, and the pipe/cable layers with the dye table.
- Covers (`gt.covers` → cover textures) are not dumped.
- Implement the UV rules above, especially ExtendedFacing rotation and flips for `extFacing` layers.
- MTEs flagged `tesr` or `customRender` (e.g. the ice cream machine has only empty faces) need special handling or
  have to be skipped.
- Pipes: per-connection geometry (arms and ends); frames combined with a pipe (`gt.blockframes` MTE bit); and
  pipe-specific overlays beyond the straight-pipe faces (e.g. restricted fluid-pipe outputs).
- Ores outside `gt.blockores2..7`, e.g. bartworks Werkstoff ores and GT++ ores. Also the Angelica/CTM variants of
  copied-block textures.
