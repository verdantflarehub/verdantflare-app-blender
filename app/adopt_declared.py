#!/usr/bin/env python3
"""Operator-only admission, run with the application stopped and old worker gone.

Read a credential-free, prevalidated migration manifest from stdin. It contains
the actor, fixed creation request and immutable legacy evidence. Do not run the
background coordinator here: the service resumes the accepted command normally.
The operator must verify Kubernetes/volume evidence and preserve old data first.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
from server import Application


def main():
    raw=sys.stdin.buffer.read(16385)
    if len(raw)>16384:
        raise ValueError('migration manifest too large')
    manifest=json.loads(raw)
    if not isinstance(manifest,dict) or set(manifest)!={'subject','organization_id','request','adoption'}:
        raise ValueError('invalid migration manifest')
    # Application startup normally quarantines interrupted scene operations.
    # An operator admission must not change unrelated in-flight work.
    with sqlite3.connect(Path(os.environ['BLENDER_DATABASE']).resolve().as_uri()+'?mode=ro',uri=True) as check:
        if check.execute("SELECT 1 FROM operations WHERE state='running'").fetchone():
            raise ValueError('scene operation still running')
    app=Application(os.environ['BLENDER_INSTANCES_FILE'],os.environ['BLENDER_DATABASE'],os.environ['BLENDER_STUDIO_TOKEN'])
    try:
        result=app.management.create(manifest['subject'],manifest['organization_id'],manifest['request'],adoption=manifest['adoption'])
        print(json.dumps(result))
    finally:
        app.store.db.close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Do not serialize manifest, upstream response bodies, environment or credentials.
        print(json.dumps({'error':getattr(exc,'code',type(exc).__name__)}),file=sys.stderr)
        sys.exit(1)
