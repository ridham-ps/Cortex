# Deploying Cortex on a server

## What Cortex keeps on disk

| What | Setting | Default | Needed by |
|---|---|---|---|
| Search index + caches (chunks, tour, walkthrough, notes) | `ONBOARDING_INDEX_DIR` | `.kt_index` | Every agent (`search_code`), the UI |
| The repo files themselves | `ONBOARDING_REPOS_DIR` | `.kt_repos` | `builtin` tool mode: Read / Grep / Glob read real files |
| Chat history | `ONBOARDING_CHAT_STORE` | JSON files in the index dir (or MongoDB) | Chat |
| Request log | `ONBOARDING_TRACE_FILE` | `trace.json` | Debugging |

When you index a **git URL**, Cortex makes a shallow clone (`--depth=1`), indexes it, and
keeps it in `ONBOARDING_REPOS_DIR/<repo>`. When you index a **local folder**, it reads the
folder in place and copies nothing, so that folder must stay where it is.

## Why not read repos straight from S3?

The agents' tools (Claude Code's Read / Grep / Glob) run on the server and need a normal
**filesystem**: grep-ing a repo means opening thousands of files quickly. S3 is object
storage, with one HTTP request per file and no grep, so it can't be the live working copy.

The working copy lives on a disk attached to the server. S3 is still useful **around** it:

| Use S3 for | How |
|---|---|
| Backups | Snapshot `ONBOARDING_INDEX_DIR` + `ONBOARDING_REPOS_DIR` (tar → S3) on a schedule |
| Repos that aren't in git (zip uploads) | Keep the zip in S3 as the original; extract it to disk and index the folder |
| Moving to a new server | Restore the snapshot instead of re-cloning everything |

## Recommended setup (AWS example)

```
EC2 instance / ECS task
  └─ persistent volume mounted at /data   (EBS for one server; EFS if several share it)
       ├─ /data/index    ← ONBOARDING_INDEX_DIR
       └─ /data/repos    ← ONBOARDING_REPOS_DIR
```

```bash
ONBOARDING_INDEX_DIR=/data/index
ONBOARDING_REPOS_DIR=/data/repos
ONBOARDING_ALLOWED_ROOTS=/data/repos          # on a server, only index clones, not arbitrary paths
ONBOARDING_CLONE_INTERACTIVE=false            # no browser on a server: fail fast without a token
ONBOARDING_BITBUCKET_TOKEN=user:app-password  # lets Re-sync re-clone private repos
CLAUDE_CODE_OAUTH_TOKEN=...                   # from `claude setup-token`
ONBOARDING_API_KEYS=<a real secret>
```

**Git is the source of truth; the disk is a cache.** If the volume is lost, nothing is
lost for good: Re-sync re-clones each repo from its git URL and rebuilds the index. A
backup to S3 just makes that recovery faster.

## Things to know

- **One server is the supported shape today.** The rate limiter and the background jobs
  (walkthrough, re-sync) live in the server's memory, so running several copies behind
  a load balancer needs shared storage (EFS) *and* moving those to shared state.
- **Disk size:** shallow clones are roughly the size of each repo's current files, and
  the index adds about the same again.
- **The disk holds your customers' source code.** Encrypt the volume (EBS/EFS
  encryption), restrict who can reach the server, and include it in your access reviews.
- **Re-sync updates both the index and the kept clone**, so the agents always read the
  same version they search.
