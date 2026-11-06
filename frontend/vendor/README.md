# Local browser dependencies

`markdown-it.mjs` is the unmodified, self-contained ESM browser bundle from
`markdown-it@15.0.3`, distributed under the MIT license in `markdown-it.LICENSE`.
It ships with the desktop frontend; no runtime CDN or Node dependency is needed.

Source: https://github.com/markdown-it/markdown-it

To update, install the exact package version as a development dependency, then copy
`node_modules/markdown-it/dist/browser/markdown-it.esm.min.mjs` to `markdown-it.mjs`
and its package `LICENSE` to `markdown-it.LICENSE`. Run the frontend rendering tests.
