# Upstream and credits

Source: [FulAppiOS/Agent-Quest](https://github.com/FulAppiOS/Agent-Quest)

Pinned commit: `736eeea65607b122ede92c502f0a6e56483c473f`

Imported source: `client/`, `server/`, `scripts/`, workspace package manifests,
`bun.lock`, and the original [MIT license](LICENSE). Upstream repository metadata,
installation scripts and GitHub workflows are not imported. The copy is maintained
here, without a nested Git repository, submodule, or automatic upstream updater.

Local modifications: server-side project membership, explicit loopback serving
and Origin checking, optional log root configuration, partial-line preservation,
factory theme and map, Korean primary labels and guide, synthetic demo and
integration tests. Global hook installation and AI-review export controls are
not exposed. External font loading and Markdown image rendering are disabled.

The original React/Phaser interaction, log parsers, state manager, WebSocket
transport, detail statistics, context/skill telemetry and map editor are reused.

Bundled Tiny Swords artwork is by Pixel Frog, under CC0 1.0:
[Tiny Swords](https://pixelfrog-assets.itch.io/tiny-swords).
Its bundled credits/license files remain with the pack. The upstream MIT license
applies to upstream code and its custom artwork. The new Patkong theme is derived
from this repository's existing public mascot drawing code; generation is in
`scripts/generate-factory.py`. It is distinct from the preserved Tiny Swords pack.

For an update, compare the pinned source with the desired upstream revision,
reapply local changes, review dependencies and licenses, then rerun all UI and
Python checks. Do not replace local modifications with an unreviewed `git pull`.
