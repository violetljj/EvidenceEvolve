# Third-party notices

## ShinkaEvolve

EvidenceEvolve optionally depends on the unmodified upstream ShinkaEvolve
package for `SHINKA_NATIVE` search execution.

- Project: ShinkaEvolve
- Repository: <https://github.com/SakanaAI/ShinkaEvolve>
- Pinned source commit: `c4568adde253cacf185be3a8412c3c2142761ebe`
- Package version: `0.0.7`
- License: Apache License 2.0

EvidenceEvolve does not vendor ShinkaEvolve source code. Its native adapter calls
the installed upstream API and writes a separate import receipt. ShinkaEvolve's
database, logs, checkpoints, configuration semantics, and WebUI artifacts remain
upstream-owned outputs.
