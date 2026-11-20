# Matrix Deep Dive setup

Matrix Deep Dive is experimental and remains disabled until the external semantic indexer is configured.

## Before enabling

Set `OPENMATRIX_XLAT_PATH` to the local `xlat.py` entry point supplied by the semantic-index project. This repository does not bundle that script. Obtain it from the project owner or its official release, then record the exact source URL, commit or release, and license in the deployment record. Review the license for redistribution and commercial-use terms before copying it into a release. Do not infer provenance from a filename.

Build an index after configuring the path:

```powershell
python $env:OPENMATRIX_XLAT_PATH index <dir> --name <index> --base .openmatrix/semantic-index
```

The backend status endpoint must report `ready: true` before the UI enables Launch Matrix Swarm. Keep the index and source documents under the configured data directory; do not commit them.
