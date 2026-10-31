"use strict";
// Spectral 1.24 calls minimatch's old default export. Keep that interface while
// using upstream's patched parser; all matching remains in upstream minimatch.
const patched = require("minimatch-patched");
module.exports = Object.assign(
  (source, pattern, options) => patched.minimatch(source, pattern, options),
  patched,
);
