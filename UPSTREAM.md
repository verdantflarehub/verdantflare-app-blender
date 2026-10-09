# Upstream provenance

The `runtime/` integration source was imported from
https://github.com/open-beagle/beagle-wind-vnc at commit
`fbe9c3f1f5d0cb7b84995ba6c3314a59a5d9ba40` (branch `kde6-blender-mcp`),
directory `KDE6/BlenderMCP`. `runtime/upstream.lock` is the corresponding
`.beagle/kde6-blender-mcp.lock`. Original source notices are retained.

This is the Blender application integration, not a fork of Blender itself.
The inspected integration source contains no root license file; this import
does not invent or replace an upstream license grant. Third-party components
retain their respective licenses.

The worker build reuses the verified upstream Blender 4.5.14/KDE6 binary base
and overlays this repository's runtime. Blender licensing and source:
https://www.blender.org/about/license/ and https://projects.blender.org/blender/blender
(tag `v4.5.14`). Distribution must retain the binary's bundled notices and
corresponding source availability. The immutable base and upstream checksums
are recorded in the Dockerfile and lock file.

Deployment and product contracts are maintained in `verdantflare-design`.
