# Comfy Cloud: `LoadVideo` widget enum can't see files uploaded via `/upload/image` (or `comfy upload` CLI)

## Summary

Third-party apps that submit workflows to Comfy Cloud can upload **images** for `LoadImage`-referenced widgets using the documented `POST /api/upload/image` endpoint (or `comfy upload --where cloud`), and everything works — the uploaded filename shows up as a valid enum option for `LoadImage` widgets and the workflow runs.

The **same endpoint accepts video files** (per the docs and comfyanonymous/ComfyUI `server.py:image_upload()`), returns a `{name, subfolder, type}` envelope, but the uploaded filename **never appears in `LoadVideo`'s enum** — so any workflow that references a user-uploaded video via `LoadVideo` fails validation with:

```
{"code": "workflow_unknown_nodes",
 "message": "Workflow has 1 validation error(s) against cloud",
 "hint": "node <id>: '<subfolder>/<hash>.mp4' not in 1 known options for file
          (did you mean: bedroom.mp4?)"}
```

`bedroom.mp4` is a video previously uploaded via the **Cloud web UI's LoadVideo "choose file to upload" button**, which registers the file with a different (proprietary) asset service. That service's contents populate the enum.

This makes any video-conditioned partner workflow (Seedance 2.0 Reference-to-Video, Depth Anything 3 Video, Kling motion-control, etc.) unreachable from third-party clients using the public CLI/HTTP surface.

## Repro

1. Fetch canonical R2V template: `templates/api_seedance2_0_r2v.json` from `Comfy-Org/workflow_templates`
2. Inject a `LoadVideo` node wired to `ByteDance2ReferenceNode.video_1` (input slot 2, socket type `VIDEO`)
3. Upload a `.mp4` (any valid one — 720p, mp4/h264, 5s, 30fps) via `comfy upload <path> --where cloud`
4. Patch the injected `LoadVideo`'s `file` widget with the returned `cloud_name`
5. Submit via `comfy run --where cloud --workflow <patched.json>`

**Expected:** workflow runs, uses uploaded video as `video_1` reference.
**Actual:** validation fails with `unknown_enum_value` for the `file` field.

Same repro with the direct multipart `POST /api/upload/image` (all four form fields: `image`, `type=input`, `overwrite=true`, and `subfolder=video`) fails identically.

## What was tried

| Attempt | Result |
| --- | --- |
| `comfy upload --where cloud` (returns `cloud_name`) | file uploads, LoadVideo enum unchanged |
| Direct `POST /api/upload/image` with `subfolder="video"` | file lands at `video/<hash>.mp4`, LoadVideo enum unchanged |
| Widget value with subfolder prefix (`video/<hash>.mp4`) | same enum error |
| Any of the other `_KNOWN_SUBFOLDER_TAGS` from `app/assets/services/path_utils.py:9` (`3d`, `pasted`, `painter`, `threed`, `webcam`) | none tag files as video |
| `type="video"` on the upload form | server coerces to `input` (per `server.py:get_dir_by_type`) |

## Root cause (as best I can tell from OSS source)

Cloud's `LoadVideo.file` widget enum is not populated from ComfyUI's own `folder_paths` scan of `input/`. It's populated from a proprietary asset catalog that only receives entries via:

1. The Cloud web UI's per-widget "choose file to upload" button (unknown endpoint)
2. Explicit partner-nodes like `ByteDanceCreateVideoAsset` (which internally calls `upload_video_to_comfyapi` → `POST /customers/storage` → an SDK-scoped signed-URL PUT → then `POST /proxy/seedance/assets`)

Neither path is reachable from `comfy` CLI or `POST /api/upload/image`.

## Suggested fixes

Any one of these would unblock third-party integrations:

1. **`comfy upload --kind video` (or `--type=video`) flag** — take a hint from the client, tag the asset appropriately in Cloud's catalog so `LoadVideo`'s enum picks it up.
2. **Expose the Cloud web UI's per-widget upload endpoint** publicly (whatever it is), and document it alongside `POST /api/upload/image`. Point third-party clients at it.
3. **A `LoadVideoFromUrl` core node** analogous to `LoadImage` but that fetches a signed URL at runtime — combined with the already-public `POST /customers/storage` this would give clients a full upload path without needing enum registration.
4. **Automatic mime-type-based tagging** in the ingest pipeline (`app/assets/services/ingest.py`): if the uploaded file's mime_type starts with `video/`, add a `video` tag so the LoadVideo enum picks it up. This would make the existing `POST /api/upload/image` work uniformly across media types.

## Workarounds attempted

- **Extract first frame → use as `image_1`.** Works, loses motion signal but Seedance runs.
- **Full `CreateVideo` chain** (extract N frames → N `LoadImage` nodes → batch → `CreateVideo` → `ByteDanceCreateVideoAsset` → `asset_1`): mechanically possible but requires ~150 injected LoadImage nodes for 5s@30fps and a first-time H5 verification step for `CreateVideoAsset`'s `group_id`. Not viable as a background/programmatic path.

## Environment

- `comfy-cli` (whatever ships in a recent Comfy Desktop bundle)
- Cloud `ByteDance2ReferenceNode` (`comfy_api_nodes/nodes_bytedance.py:2098`)
- Third-party app in Python, POSTing multipart form to `<cloud_base>/api/upload/image`

## Impact

Blocks any third-party editor / pipeline that wants to feed user-recorded or programmatically-generated video into a video-conditioned Cloud partner node. Currently the only way for our users to run Seedance 2.0 R2V with their own footage is to drag-drop it into the Cloud web UI manually, which defeats the purpose of an app-driven workflow.
