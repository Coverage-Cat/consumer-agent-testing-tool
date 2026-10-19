# JSON and YAML data

Configuration documents may use JSON or YAML. Files ending in `.json`
(case-insensitively) must contain valid JSON. The initializer writes JSON for
those filenames and YAML otherwise.

The shared loader decodes valid JSON with the JSON parser before considering
YAML syntax. This preserves scientific-notation numbers as numbers and decodes
escaped Unicode characters consistently with the JSON writers. Unicode strings
are not normalized: composed and decomposed spellings remain distinct.

Duplicate keys, non-finite numbers and nesting deeper than 64 levels are
rejected. A semantic JSON error does not retry through YAML. YAML configuration
retains the existing scenario rules: `on` and date labels remain strings, while
`true` and `false` are booleans.

Evidence redaction replaces content nested deeper than 64 levels with a marker,
so every sealed document reloads. Model tool proposals may contain NaN or
Infinity, which Python's JSON parser accepts. Evidence records them as the
strings `"NaN"`, `"Infinity"` and `"-Infinity"`; the approver still sees the
original numbers and rejects the proposal as having invalid arguments.

This matters when reopening saved locks, checkpoints and evidence. Reinterpreting
a number as a string can change a replay request or digest. Decoding an escaped
Unicode character differently can corrupt a name or prevent hashing. The tests
exercise exact contract replay, rejection of changed arguments, sealed playback,
public export and generated configuration files with these values.

Relevant checks are in `tests/test_serialization.py`.
