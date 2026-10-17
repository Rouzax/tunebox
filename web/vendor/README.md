# Vendored files

Fetched by hand from the npm registry, tarball integrity checked against the
registry, no package scripts run. `SHA256SUMS` is checked by
`web/tests/vendor.test.js`.

| Folder | Source | Licence |
|---|---|---|
| `audiomotion-analyzer-4.5.4/` | `audiomotion-analyzer` 4.5.4, `src/audioMotion-analyzer.js` (the ES module build) | AGPL-3.0-or-later |
| `poppins-5.3.0/` | `@fontsource/poppins` 5.3.0, latin and latin-ext woff2, weights 300, 400, 500 | SIL Open Font License 1.1 |

To update: fetch the new tarball the same way, replace the folder (its name
carries the version, which is also what makes browsers fetch it again),
regenerate `SHA256SUMS`, and update the import path in `web/js/visualizer.js`
or `web/css/fonts.css`.
