# Vendored files

npm packages are fetched by hand from the npm registry, tarball integrity
checked against the registry, no package scripts run. `SHA256SUMS` is checked by
`web/tests/vendor.test.js`.

| Folder | Source | Licence |
|---|---|---|
| `audiomotion-analyzer-4.5.4/` | `audiomotion-analyzer` 4.5.4, `src/audioMotion-analyzer.js` (the ES module build) | AGPL-3.0-or-later |
| `poppins-5.3.0/` | `@fontsource/poppins` 5.3.0, latin and latin-ext woff2, weights 300, 400, 500 | SIL Open Font License 1.1 |
| `qrcode-generator-2.0.4/` | `qrcode-generator` 2.0.4, `dist/qrcode.mjs` (the ES module build), for the admin setup page | MIT |
| `material-symbols-rounded-737e332/` | Material Symbols Rounded, weight 400, 24 px, from Google's `google/material-design-icons` repository at commit `737e332` (Google publishes no npm package for them); only the icons in `scripts/icons.txt`, plus `icons.svg` built from them | Apache-2.0 |

To update: fetch the new tarball the same way, replace the folder (its name
carries the version, which is also what makes browsers fetch it again),
regenerate `SHA256SUMS`, and update the import path in `web/js/visualizer.js`
or `web/css/fonts.css`.

`audiomotion-analyzer` is also pinned in `web/package.json`, so Dependabot
opens a pull request when a new version is published. That pull request
changes only the pin and fails `vendor.test.js` ("... is vendored") on
purpose, and it is never merged automatically: vendor the new version as
above on its branch, update the pin's folder in `VERSIONED_FILE` if the file
name changed, and the check passes. Nothing is installed from
`web/package.json`; it exists for Dependabot and the test.

Icons: edit `scripts/icons.txt` and run `.venv/bin/python scripts/vendor_icons.py`.
It fetches from the pinned commit, rebuilds `icons.svg` and rewrites that
folder's lines in `SHA256SUMS`.
