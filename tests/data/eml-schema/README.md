# EML schema files

The official XML Schema files for each EML version the parser supports, used by `tests/test_eml_schema.py`
(issue #29). They are test data only and are not part of the installed package.

| Version | Source | Commit |
|---|---|---|
| 2.1.1 | [NCEAS/eml](https://github.com/NCEAS/eml) tag `RELEASE_EML_2_1_1`, repository root (`eml*.xsd`, `stmml.xsd`) | `e80ac91171fc7b34da72f1dd0b790498738fc899` |
| 2.2.0 | [NCEAS/eml](https://github.com/NCEAS/eml) tag `RELEASE_EML_2_2_0`, `xsd/` | `233427f03610694d2a865e6748bcf8bfb51be900` |

The files are unmodified. They are copyright the Regents of the University of California and distributed under
the GNU General Public License, version 2 or later, as stated in each file's header.

## Adding a version

1. Copy the release's `.xsd` files into a new folder named after the version.
2. Add the version's root namespace to `SUPPORTED_NAMESPACES` in `src/arcticdata_mcp/eml.py`.
3. Run `uv run pytest tests/test_eml_schema.py`. It lists every new element or attribute that no section returns;
   handle each in `eml.py` or add it to `IGNORED` (with the reason) or `KNOWN_GAPS`.
