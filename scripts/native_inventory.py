"""Observe native payloads from explicit installed-file inventories; never execute them."""
import hashlib
import json
import stat
import struct
from pathlib import Path

from distribution_support import contained


def payload_format(stream, size):
    """Recognize executable/font headers independently of filename extensions."""
    header = stream.read(64)
    magic = header[:4]
    if header[:8] == b"\0asm\x01\0\0\0":
        return "webassembly"
    if magic == b"\x7fELF" and len(header) >= 20 and header[4] in (1, 2) and header[5] in (1, 2):
        return "elf"
    if magic in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                 b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe") and len(header) >= 28:
        return "mach-o"
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                 b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca") and len(header) >= 8:
        endian = ">" if magic[0] == 0xca else "<"
        count = struct.unpack(endian + "I", header[4:8])[0]
        stride = 32 if magic in (b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca") else 20
        # A Java class also starts CAFEBABE. Validate the bounded fat-image table
        # and each slice's own header instead of classifying by magic alone.
        if not 0 < count <= 64 or 8 + count * stride > size:
            return None
        stream.seek(8)
        table = stream.read(count * stride)
        for index in range(count):
            row = table[index * stride:(index + 1) * stride]
            offset, length = struct.unpack(endian + ("QQ" if stride == 32 else "II"), row[8:24 if stride == 32 else 16])
            if offset < 8 + count * stride or length < 28 or offset + length > size:
                return None
            stream.seek(offset)
            if stream.read(4) not in (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                                      b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"):
                return None
        return "mach-o-universal"
    if header[:2] == b"MZ" and len(header) == 64:
        offset = struct.unpack("<I", header[60:64])[0]
        if 64 <= offset <= min(size - 4, 1024**2):
            stream.seek(offset)
            if stream.read(4) == b"PE\0\0":
                return "pe"
    return {b"OTTO": "opentype", b"\x00\x01\x00\x00": "truetype",
            b"ttcf": "font-collection", b"wOFF": "woff", b"wOF2": "woff2"}.get(magic) \
        if len(header) >= 12 else None


def observe(root, path, *, expected=None):
    root = Path(root).resolve()
    path = contained(root, path).resolve()
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Native inventory requires regular installed files")
    with path.open("rb") as stream:
        kind = payload_format(stream, before.st_size)
        checksum = None
        if kind is not None or expected is not None:
            stream.seek(0)
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
        raise ValueError("Native payload changed during inventory")
    if expected is not None and checksum != expected:
        raise ValueError("Native payload differs from the installed-file inventory")
    if kind is None:
        return None
    return {"sha256": checksum, "bytes": before.st_size, "format": kind,
            "kind": "binary" if kind in ("elf", "mach-o", "mach-o-universal", "pe", "webassembly") else "font"}


class NativeInventory:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.components = {}
        self.seen = set()
        self.observed_groups = set()

    def add(self, group, identity, paths, *, documents=(), package=None):
        """paths maps stable package-relative names to (workspace path, known SHA)."""
        self.observed_groups.add(group)
        if identity in self.seen:
            raise ValueError("Duplicate native inventory component")
        self.seen.add(identity)
        artifacts = {}
        # Browser distributions can expose aliases within their own bundle.
        # Preserve aliases but hash each canonical payload only once per component.
        observed = {}
        for name, (path, expected) in sorted(paths.items()):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Native artifact names must be component-relative")
            resolved = contained(self.root, path).resolve()
            if resolved not in observed:
                observed[resolved] = observe(self.root, resolved, expected=expected)
            result = observed[resolved]
            if result:
                if expected is not None and result["sha256"] != expected:
                    raise ValueError("Native alias differs from the installed-file inventory")
                artifacts[name] = result
        if artifacts:
            self.components[identity] = {"scope": group, "artifacts": artifacts,
                "package": package,
                "upstream_documents": list(documents),
                "dependency_coverage": {"status": "unverified",
                    "reason": "Payload hashes and package documents do not establish embedded dependency completeness"},
                "notice_coverage": {"status": "unverified",
                    "reason": "Original notices for embedded components and fonts need separate review"}}

    def python(self, group, installation, components):
        indexed = {row["name"].lower().replace("_", "-"): row for row in components}
        prefix = contained(self.root, installation["prefix"]).resolve()
        self.observed_groups.add(group)
        for package in installation["packages"]:
            name = package["name"].lower().replace("_", "-")
            component = indexed[name]
            properties = {row["name"]: row["value"] for row in component["properties"]}
            paths = {}
            for name in package["files"]:
                path = contained(self.root, name).resolve()
                if not path.is_relative_to(prefix):
                    raise ValueError("Installed Python payload escapes its selected environment")
                if path.suffix in (".pyc", ".pyo"):
                    continue  # Disposable interpreter caches are not wheel payloads.
                if not path.is_file():
                    raise ValueError("Listed installed Python file is missing")
                paths[path.relative_to(prefix).as_posix()] = (path, None)
            self.add(group, component["bom-ref"], paths,
                     documents=json.loads(properties["consumer-agent-testing-tool:upstream-sboms"]),
                     package={"name": package["name"], "version": package["version"]})

    def result(self):
        missing = sorted({"core-python", "optional-api-python", "node-protocol",
                          "node-browser", "node-api", "browser-runtime"} - self.observed_groups)
        gaps = [{"scope": scope, "reason": "Required installation was not inventoried"} for scope in missing]
        for name, component in sorted(self.components.items()):
            gaps.extend({"scope": name, "reason": component[field]["reason"]}
                        for field in ("dependency_coverage", "notice_coverage"))
        gaps.extend([
            {"scope": "host-runtime", "reason": "System interpreters, shared libraries and system fonts outside the workspace were not inspected"},
            {"scope": "worker-images", "reason": "Current container payload dependencies and notices are not reconciled by this installed-file inventory"},
        ])
        artifacts = [row for component in self.components.values() for row in component["artifacts"].values()]
        return {"schema_version": 1, "status": "unverified",
                "scope": "Observed installed native payloads and fonts, separate from package-level SBOMs",
                "observed_groups": sorted(self.observed_groups),
                "components": dict(sorted(self.components.items())),
                "summary": {"components": len(self.components), "artifact_paths": len(artifacts),
                            "unique_payload_sha256": len({row["sha256"] for row in artifacts}),
                            "font_paths": sum(row["kind"] == "font" for row in artifacts)},
                "gaps": gaps,
                "limits": "No inference of embedded components from filenames or package license strings; no external filesystem scanning."}
