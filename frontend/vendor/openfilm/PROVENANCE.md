# OpenFilm subtitle segmentation

Source: [openfilm/openfilm](https://github.com/openfilm/openfilm/blob/ea8cf6dcbb367065ce16d75e9784dbe2ab3e013c/packages/openfilm/studio/server/subtitle-segments.mjs), version 0.2.6, commit `ea8cf6dcbb367065ce16d75e9784dbe2ab3e013c`.

`subtitle-segments.mjs` is an unmodified copy of the dependency-free, I/O-free upstream module. `SHA256.json` pins its content and the retained MIT `LICENSE`. `subtitle-segments.d.mts` provides local TypeScript declarations; the platform adapter lives in `lib/subtitle-segmentation.ts`.

Segmentation uses text-derived estimated times. It does not perform speech recognition or forced alignment.
