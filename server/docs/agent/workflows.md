# Workflow modules — creating, importing, and repairing

This doc covers the `create_workflow_module` path in full. Load it when a user asks you to build, import, or fix a workflow. Everyday scene edits do NOT need this doc.

## Naming

Keep the module `label` short enough to fit on ONE line in a ~200px cell — roughly 24 chars. Use `·` (middle dot) as a separator and prefer abbreviations: T2I / I2I / T2V / I2V / T2M / I2M / T2S / Depth / Upscale / Remove BG / Extend / etc. Include "Local" or "Cloud" only when both variants might exist.

- Good: `Flux 2 Klein · T2I · Local`, `SDXL · I2I`, `Tripo · I2M`
- Bad: `Flux 2 Klein — Text to Image (Local)`

## HARD RULE — NEVER RECONSTRUCT A KNOWN PARTNER WORKFLOW BY HAND

If the target model has an official Comfy Cloud template (BFL, ByteDance, Kling, Ideogram, Runway, Stability, OpenAI, Vertex/Nano Banana, Recraft, Reve, xAI/Grok, Luma, Pika, Vidu, Moonvalley, Hailuo, etc. — anything under `Comfy-Org/workflow_templates`), you MUST fetch the canonical JSON and use it verbatim. Do NOT hand-build the graph from what you assume the node shape looks like.

**Why:** Partner-API nodes (`ByteDanceSeedreamNodeV2`, `KlingImageNodeV2`, etc.) have widget orders, socket names, and input-conversion shapes (`shape: 7`) that are NOT self-evident from the node name. A hand-built API-format graph will pass local validation but hit "Failed to validate images" / `shape_mismatch` / other opaque cloud errors that get misdiagnosed as "widget_index unreliable" or "partner API rejects the input" — neither of which is true. The canonical template already has the exact wiring the cloud validator expects.

**How to fetch canonically (in priority order):**
1. Comfy Cloud MCP `search_templates` → `get_template` — returns the exact API/graph JSON the Cloud team ships.
2. Direct GitHub raw fetch: `https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/api_<partner>_<model>_<mode>.json` — where mode is typically `t2i`, `image_edit`, `i2v`, `t2v`, etc.
3. Only as last resort (custom nodes with no template): construct by hand, and document the source you pattern-matched against.

**How to identify hand-built damage:** If a partner-API workflow errors at Cloud validation and the JSON was written by an agent (not fetched from templates), the fix is almost never "tweak the widget_index" — it's "throw the whole file away and refetch the canonical template." Retro-fitting is slower than starting over.

**Precedent to point at:** `seedream_5_pro_image_edit` — an earlier agent hand-built it in API format, then spent multiple sessions inventing wrong root-causes (widget_index instability, `ByteDance* nodes can't be patched`, "cloud validator hard-rejects INT") to explain "Failed to validate images." The real cause was the reconstructed graph diverging from the canonical template. Fixed by swapping in `api_bytedance_seedream_5_0_pro_image_edit.json` verbatim.

## The 8-step recipe

**1. Get the workflow.** Fetch a matching template via the Comfy Cloud MCP `get_template` tool. Only fall back to hand-construction when `search_templates` returns nothing — and even then, prefer downloading a similar partner's template as a shape reference over inventing from the class_type name alone. Ask `search_templates` first to find a match.

**2. Decide user-facing inputs.** Expose ONLY what changes per run (prompt, seed if the user cares, source image for image-edit workflows). Everything else stays baked into the workflow.

**3. For each input, work out the node id + widget slot to patch.** Include both `widget_index` (0-based) AND `widget_name` (e.g. `"text"` for CLIPTextEncode) — the cloud runner uses index, the local runner uses name.

### 3a. Latent aspect ratio

For workflows with a `scene-image` input, the local runner auto-patches the EmptyLatentImage / EmptySDXLLatentImage / EmptySD3LatentImage width and height at run time to match the viewport snapshot's aspect ratio (long side preserved, snapped to /64). Don't hand-code a square 1024×1024 assuming the user's viewport is square — leave the workflow's authored resolution and the runner will reshape it. Only override if you specifically want to lock a resolution.

### 3b. Seed + strength widgets

- Any KSampler → expose seed as `type: "seed"` (the UI adds a 🎲/🔒 random-vs-fixed toggle — random by default, user can lock a seed they liked). **NEVER `type: "number"` for seeds** — the UI stores number-field values as strings, and cloud partner nodes (`ByteDanceSeedreamNodeV2`, `KlingImageNodeV2`, etc.) hard-fail with `shape_mismatch` when they receive a string where INT is expected. `type: "seed"` triggers the runner's int-coercion path in both `_workflow_local.py` and `_workflow_shared.py:run_cloud`.
- ControlNet / IPAdapter / LoRA strength widgets that materially affect output (typical 0-1 range) → expose as `type: "number"` with `default`, `min: 0`, `max: 1`, `step: 0.05`. `type: "number"` gets float-coerced before submission — cloud FLOAT validators accept it.
- Same treatment for CFG when it's not baked in.

These are the two most-common per-run knobs; skipping them forces the user back into the raw workflow JSON.

### 3d. Multi-widget dropdowns (value_map)

One dropdown can patch multiple widgets with different values via `patch: [...]` + `value_map`. Example: a "Lightning" selector on a LoRA node writes the correct lora filename AND toggles strength_model to 0 for "Off":

```json
{
  "name": "lightning",
  "type": "dropdown",
  "options": ["Off", "4-step", "8-step"],
  "default": "4-step",
  "patch": [
    { "node_id": 96, "widget_name": "lora_name",
      "value_map": {
        "Off": "Qwen-Image-Lightning-4steps-V1.0.safetensors",
        "4-step": "Qwen-Image-Lightning-4steps-V1.0.safetensors",
        "8-step": "Qwen-Image-Lightning-8steps-V1.0.safetensors"
      } },
    { "node_id": 96, "widget_name": "strength_model",
      "value_map": { "Off": 0.0, "4-step": 1.0, "8-step": 1.0 } }
  ]
}
```

Supported by both `run_local` and `run_cloud`. Use when you want ONE knob to gate a whole node's behavior (bypass, mode switch, quality preset).

### 3c. Preprocessor previews

If the workflow has a visual preprocessor stage (depth, canny, pose, normal, seg, lineart, HED, MiDaS, Zoe, Marigold, OpenPose, etc.), declare it under `intermediates`:

```
[{name, label, source_node_id, source_slot}]
```

The runner splices a SaveImage onto that node and the editor shows a preview tab (e.g. DEPTH) between BLOCKOUT and RENDER, so the user can compare the preprocessor output against the final image. `source_node_id` is the preprocessor's numeric id; `source_slot` is 0 for its main image output.

**4. Workflow format.** Both API and graph/save are supported by the cloud + local runners. **STRONGLY PREFER API FORMAT** for new modules (flat dict keyed by node id string, each entry has `class_type` + `inputs` dict, no widget positional counting, no shape:7 shift bugs).

- The Comfy Cloud web UI's "Save (API Format)" export IS this format.
- Cloud's templates ship as API format, so MCP `get_template` returns it directly — no conversion step.
- The cloud runner's manifest patcher (`_find_node` in `server/modules/_workflow_shared.py`) auto-detects format and patches by `widget_name` (dict key) for API and `widget_index` (list position) for graph.

**FASTEST PATH TO A NEW WORKFLOW MODULE:**
1. `search_templates` to find a canonical Comfy Cloud template for the model
2. `get_template` to download the API-format JSON
3. Write a matching meta.json where each manifest patch has `node_id` + `widget_name` (the input key from the template — e.g. `"image"` for LoadImage)
4. Call `create_workflow_module` — done, no widget-counting, no format arguments

Legacy graph/save format (`last_node_id` + `nodes[]` array + `links[]` at root) still runs — see `server/workflows/tripo_p1_i2m_cloud.json` for a reference graph-format cloud workflow. Local runner accepts both formats.

**5. Call `create_workflow_module`.** The workflow cell appears in the WORKFLOWS section immediately — no reload needed.

To FIX or REPLACE an existing workflow module (wrong text encoder, missing node, agent mistake in the first pass): call `get_workflow_module` with its id → see current JSON → work out what needs to change → re-call `create_workflow_module` with the same id and the corrected workflow. The register endpoint overwrites atomically and hot-registers the updated module in place.

**6. If runner="local", check custom nodes.** IMMEDIATELY call `check_custom_nodes` with every third-party node class_type the workflow references (skip built-ins: KSampler / CLIPTextEncode / VAEDecode / etc.). For every missing repo, IMMEDIATELY call `install_custom_node` yourself — the tool clones AND auto-pips the requirements against ComfyUI's own Python. Do NOT hand the user manual `git clone` or `pip install` commands.

If `pip_ok: true` (deps installed), IMMEDIATELY call `restart_comfy` — do NOT ask the user to Ctrl+C. Only fall back to asking the user manually when the tool reports `pip_ran: false` (ComfyUI's Python interpreter wasn't detected) or `restart_comfy` returns restarted=false with a manager-missing error.

**7. Check local models.** Call `check_local_models` with every model file (checkpoints, VAEs, text encoders, LoRAs, etc.). Format the result as a compact bulleted list (the chat panel is narrow — NO wide tables). For any missing model, group by folder and give a short HuggingFace slug, then ask "want me to download it for you?" before doing anything.

**8. Download models on confirmation.** Call `download_model_to_comfy` with the direct HF URL (`https://huggingface.co/<repo>/resolve/main/<path>`), the ComfyUI folder, and the exact filename. Multiple missing models = call the tool sequentially, one per file — don't parallelize; the server writes `.part` files that could collide.

## Import UX (user has a workflow JSON to add)

The AI Agent chat input row has an Import Workflow button (tray icon, top-right of the textarea) that opens a file picker for `.json`, reads it, and auto-injects a directive prompt. If a user says "I have a workflow to import" or "can you add this workflow for me", **point them at that button first** — it's faster and cleaner than asking them to paste a big JSON blob into chat.

When the button fires, you receive the file's contents in a fenced ```json``` block with the directive already spelled out (identify format, pick label, identify inputs, patch by widget_name for API, ask before creating if anything is ambiguous). Confirm ambiguous decisions with the user in one round before calling `create_workflow_module`.

## Scene-image input — THE universal image-input pattern

Every image-in workflow in ComfyBlockout uses the SAME pattern. There is no other pattern. Learn it once and apply it to every module you build:

**The mechanism.** The workflow.json has a `LoadImage` node with `image: "scene_snapshot.png"` (a placeholder filename). The manifest declares an input `{"type": "scene-image", "patch": {"node_id": <LoadImage id>, "widget_name": "image"}}`. At run time:

1. The runner resolves the image source — see next section.
2. The runner uploads that file to Comfy Cloud (`upload_image_to_cloud`) or copies it into ComfyUI's `input/` dir (local).
3. The returned filename is patched into the LoadImage node's `image` widget, replacing `scene_snapshot.png`.
4. LoadImage decodes it into an IMAGE tensor that flows to the compute node via a link.

**Where the image comes from — viewport OR custom, always.** The workflow cell's image slot is a single UI control that resolves to one of two sources:

- **Empty slot** ("Empty = uses current viewport") → the runner captures the current 3D viewport (blockout render, PNG, matching the current AR) and uses THAT as `image_path`. This is the default and the whole point — the blockout scene is the image reference for most runs.
- **Populated slot** (user clicked/dragged an image onto the slot) → that uploaded file is used as `image_path` instead. Same downstream path — upload → patch → LoadImage decode.

Both are the same input from the module's perspective. The `scene-image` type IS "viewport OR custom, whichever is present."

**Wiring rules for every workflow you build:**

**(a)** INCLUDE a LoadImage node in the graph, even if the source model has its own image input. Always route through LoadImage — it's the only path the runner knows how to feed.

**(b)** The manifest patch MUST target LoadImage's `image` widget (API: `widget_name: "image"`; graph: `widget_index: 0`). NEVER patch the compute node's image socket directly — sockets expect a decoded IMAGE tensor, and the runner only knows how to write a filename string into a widget.

**(c)** API format: the compute node's `image` input is a link ref `["<loadimage_id>", 0]` — output 0 of LoadImage is IMAGE, output 1 is MASK.

**(d)** Graph format: same wiring via top-level `links[]` as `[<link_id>, <loadimage_id>, 0, <compute_id>, <input_slot>, "IMAGE"]`.

Wiring wrong → the compute node receives a filename string where a tensor is expected → runtime error `'str' object has no attribute 'shape'`.

**Multi-LoadImage workflows (chained style refs, dual-image edits, etc.).** If a workflow has TWO or more LoadImage nodes that should both receive the viewport/custom image (e.g. Krea 2's chained `Krea2StyleReferenceNode` pair), declare a separate manifest input per LoadImage (`source_image`, `source_image_2`, ...), each with its own patch targeting that LoadImage's `image` widget. The runner reads `kwargs["image_path"]` for every `scene-image` input, so all of them get the SAME uploaded file — the UI still shows ONE slot to the user (that's fine; the whole point is one source fanning out).

Only declare extra `scene-image` inputs if extra LoadImage nodes actually exist in the graph. If you need two DIFFERENT source images per run, that's not supported by the current single-`image_path` runner — you'd need to split into two runs or extend the runner.

## Before diagnosing "node X has no scene-image patch" — READ THE META FIRST

If a partner-API cloud run fails and you're about to claim "node N is a LoadImage with the literal `scene_snapshot.png` and no patch on it, so the validator rejects it":

**STOP. Open the module's `.meta.json` and grep for the node id.** If the meta already declares an input with `patch.node_id == N` and `type: "scene-image"`, node N IS patched at runtime — the literal `scene_snapshot.png` you see in the .json is the placeholder, which the runner replaces. The failure is somewhere else (wiring topology, widget order, canonical-template divergence — see the HARD RULE at the top of this doc).

Adding a "second patch" or "duplicate scene-image input" for a node that already has one is a fake fix. Re-read the meta before proposing it.

**Support answers:**
- "Why is my workflow using the scene instead of the image I picked?" → the cell.values for the image key isn't set (they may have clicked Generate before the upload completed, or the drop failed silently).
- "How do I feed a reference image?" → click/drop onto the workflow cell's image slot.

## Cloud gotchas

### Shape mismatch (`shape_mismatch` / string-where-int-expected) — READ THIS BEFORE DIAGNOSING

If a cloud run fails with `shape_mismatch`, `expected INT got STRING`, `expected FLOAT got str`, or similar type-shape errors on a numeric widget (seed, steps, cfg, strength):

**ROOT CAUSE (99% of the time):** the manifest input `type` is wrong. HTML `<input>` stores all values as strings; only `type: "seed"` (→ int coercion) and `type: "number"` (→ float coercion) get cast before submission. Both runners now coerce identically — `_workflow_local.py:_apply_single_patch` for local, `_workflow_shared.py:run_cloud`'s else-branch for cloud (added 2026-07-31). `type: "text"` or missing type = string sails through and cloud validator rejects it.

**CORRECT FIX:** change the manifest `type` (usually to `"seed"` for KSampler seed, `"number"` for cfg/strength/denoise). ONE LINE EDIT. Restart run.bat to reload the manifest cache. Test.

**DO NOT DO ANY OF THE FOLLOWING** (these were tried, they are wrong, they will regress the user's control):

1. Do NOT hardcode the seed/number into the workflow JSON and strip the input from the manifest as a "workaround." That hides the real bug, removes user control, and gaslights the next agent into thinking the pattern is intentional.
2. Do NOT claim "the patch path is ambiguous because widget_index is unreliable on partner-API nodes." There is ONE patch function per runner. It's ~15 lines. It patches by widget_name for API format. There is no ambiguity, no dynamic-combo instability layer, no "partner node patch path." The coercion is driven purely by `input_type` — nothing else.
3. Do NOT invent explanations like "ByteDance* nodes have known issues" or "cloud validator hard-rejects regardless of manifest type." They don't. This specific class of error is a manifest-type bug in the user's meta.json, always. Read the actual `_workflow_shared.py:run_cloud` source before speculating about behavior.
4. Do NOT treat this as a "permanent limitation." If `type: "seed"` isn't working after a `run.bat` restart, the meta wasn't reloaded — remind the user (per `feedback_manifest_reload_needs_restart` in the codebase memory).

**Sanity check before proposing any workflow-meta change:** open `server/modules/_workflow_shared.py` and read `run_cloud` (~30 lines). Open `server/modules/_workflow_local.py` and read `_apply_single_patch` (~60 lines). Both coerce on `input_type in ("seed", "number")`. That's the whole story.

### "Failed to validate images" (Cloud LoadImage rejects the upload)

Cloud's `LoadImage` node only accepts `.png` / `.jpg` / `.jpeg`. Viewport snapshots are now always PNG at the source (`web/editor.html:autoSnapshot` uses `canvas.toBlob(..., "image/png")`) — no per-runner transcode.

If you see "Failed to validate images" (visible in the Comfy Cloud web dashboard, NOT the CLI's generic `execution_error` summary):

1. Verify snapshots are still PNG — grep `autoSnapshot` in `web/editor.html` for the `toBlob` call.
2. Rule out size / dimension limits — Cloud may cap `LoadImage` at some pixel or byte ceiling. Check the file that landed in `DATA_DIR/node_<id>_image.png` before assuming it's a format issue.
3. Do NOT reintroduce WebP for snapshots without verifying against a real cloud workflow first — this was tried and reverted (see `feedback_webp_for_viewport_snapshots.md`).

### Widget-position shift (shape:7)

When a graph converts a widget into a socket (input entry has `shape: 7` in the node's `inputs[]` array), the cloud validator STILL COUNTS THAT WIDGET SLOT when reading `widgets_values`, but the graph JSON no longer stores a value for it. Result: every widget AFTER the converted one gets shifted -1 relative to what cloud expects — cloud reads pose_mode where you wrote seed, reads seed where you wrote seed_control, etc.

**Fix:** INSERT an empty-string entry (`""`) in `widgets_values` at the position where the converted widget would have lived.

**Diagnose from the error:** if cloud reports `field: pose_mode, code: unknown_enum_value` with a numeric value that matches a seed-shaped INT one slot later in your list, you've hit this shift. Look at the node's `inputs[]` for any `shape: 7` entries and count how many slots to insert (one per converted widget). Meshy 6 · I2M in the repo hit this — the `should_texture.texture_image` socket needed an inserted `""` at index 8. Rodin 3D similarly has multiple `shape: 7` inputs that would each need an empty slot if converted.

### Cloud 3D catalog gap

`comfy generate list` shows only image + video partners (bfl, kling, vertexai/nano-banana, seedance, etc.). Meshy, Rodin, Tripo are NOT in the partner-generate catalog — they're custom nodes only. So for 3D generation you MUST use the graph-workflow submission path — you cannot bypass to a `comfy generate <model>` CLI call the way `/api/skybox/generate` does for Flux 2.

If a user asks "can we just hit an endpoint for 3D like skybox does?", explain this catalog gap: 3D partners aren't wired to `comfy generate`, so workflow submission is the only route.

### Diagnosing runtime errors (validation passes, execution fails)

Use the Comfy Cloud MCP `get_node` tool (`mcp__plugin_comfy-cloud_comfy-cloud__get_node`) to inspect the failing node's REQUIRED input types + widget specs. Especially useful for API nodes (Meshy, Rodin, Kling, Ideogram, Flux Pro, etc.) — some declare `IMAGE` as their input type but internally accept a filename string (from Cloud's LoadImage upload) or a URL, then call their own `upload_images_to_comfyapi` helper that requires an actual tensor with `.shape`.

If you see `'str' object has no attribute 'shape'` in the traceback, this is the class of bug: the API node's execute path isn't happy with what LoadImage handed it.

Compare to a WORKING sibling (e.g. Tripo I2M uses the same LoadImage → node pattern — check `server/workflows/tripo_p1_i2m_cloud.json`). If the graph structure matches but one fails at runtime, it's a bug in that specific partner's cloud implementation, not a workflow shape issue.

Options:
- File the bug + wait for a fix
- Try an alternate cloud API node for the same task if one exists (`search_nodes` MCP tool)
- Preprocess the image differently (some nodes want it fed via a PreviewImage or explicit VAEDecode step)

## Local runner diagnostics

For any local workflow question, the ComfyUI Desktop instance at `http://127.0.0.1:8188` is authoritative. Prefer it over guessing node names or widget schemas.

### Inspect what classes exist

```bash
curl -s http://127.0.0.1:8188/object_info | python -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(k for k in d if 'YourSearch' in k))"
```

Common gotchas:
- Node display names (`Load Video FFmpeg (Upload)`) do NOT necessarily match class names (`VHS_LoadVideoFFmpeg`). Don't guess — query.
- VHS ships multiple LoadVideo variants (`VHS_LoadVideo`, `VHS_LoadVideoPath`, `VHS_LoadVideoFFmpeg`, `VHS_LoadVideoFFmpegPath`). Upload variants take a filename enum widget; Path variants take an absolute filesystem path. Our runner uploads scene-video to Comfy's `input/` dir → use the Upload variant.

### Inspect a node's widget schema

```bash
curl -s http://127.0.0.1:8188/object_info/CLASS_NAME | python -c "import json,sys; d=json.load(sys.stdin); ins=d.get('CLASS_NAME',{}).get('input',{}); print('REQUIRED:', list(ins.get('required',{}).keys())); print('OPTIONAL:', list(ins.get('optional',{}).keys()))"
```

Every field named under `required` MUST appear in the workflow.json's `inputs` — missing any triggers `Required input is missing: <name>` at validation time. Optional inputs (`meta_batch`, `vae`, `format`, etc.) can be omitted.

### Read the ComfyUI terminal

When a local run fails, the ComfyUI Desktop LOGS panel has the real traceback. Ask for it — the error dialog in the editor only shows the runner's polling error, which is usually a downstream symptom, not the root cause.

Common patterns seen from the terminal:
- `Value not in list: lora_name: '<file>' not in (list of length N)` — model file missing from `models/loras/`. ComfyUI validates every node upfront including branches a switch would skip; there's no lazy validation. Downloading the model or renaming to match the workflow.json is the only fix. See "eager validation" below.
- `Required input is missing: <name>` — the node has a new required widget your workflow doesn't set. Query `/object_info/<class>` and add the field with a sensible default.
- `Sizes of tensors must match except in dimension 1. Expected size 1 but got size N` — batch/conditioning mismatch. See "video-output workflows" below.

### Eager validation caveat

ComfyUI validates ALL nodes at prompt submission time, including branches that a runtime switch (`ComfySwitchNode`, `Impact Switch`, etc.) would prune. So a LoraLoader referencing a missing file kills the whole prompt even when its switch is off. This is why UI toggles cannot gate model-file requirements without a runner-side node-bypass helper.

## Dynamic widget dropdowns (`options_source`)

For any dropdown whose valid options depend on the live ComfyUI catalog (samplers, schedulers, model names, checkpoint files, LoRA files, etc.), don't hardcode them. Use `options_source`:

```json
{
  "name": "sampler",
  "type": "dropdown",
  "options_source": { "class_type": "KSampler", "widget": "sampler_name" },
  "default": "euler",
  "patch": { "node_id": "30:3", "widget_name": "sampler_name" }
}
```

The frontend fetches the current enum list from ComfyUI's `/object_info/<class_type>` at render time and populates the menu. Prevents stale hardcoded lists that lose new samplers as ComfyUI adds them. Reference implementations: `flux_dev_i2i_lora_local.meta.json`, `sd15_lora_t2i.meta.json`, `krea2_turbo_local_anim.meta.json` (sampler + scheduler).

## Video-output workflows (batch cascade)

Workflows that emit a video (kind: "video", output_ext: "mp4") need one of two patterns:

### Pattern A — Native batch model (WAN, AnimateDiff, etc.)

The UNET handles batched conditioning natively. Standard graph:
`LoadVideo → VAEEncode → KSampler (batch-native) → VAEDecode → SaveVideo / VHS_VideoCombine`

Conditioning at batch=1 broadcasts to the image batch. No special handling.

### Pattern B — Non-batch UNET (Krea 2, most image models) — REQUIRES meta_batch

Krea 2's forward pass does `torch.cat((context, img), dim=1)` which hard-fails when `context.shape[0] != img.shape[0]`. Feeding a 12-frame image batch through a KSampler with a 1-sample conditioning throws `Sizes of tensors must match except in dimension 1. Expected size 1 but got size 12`.

Fix: wire `VHS_BatchManager` (from VideoHelperSuite) with `frames_per_batch: 1`. Each sub-execution processes exactly one frame, so conditioning-batch (1) matches image-batch (1) and Krea 2's cat succeeds. VHS auto-requeues the workflow N times (once per frame) and accumulates outputs into a single final mp4.

Wiring:
- Add a `VHS_BatchManager` node (id "52" by convention in existing workflows) with `frames_per_batch: 1`.
- Add `"meta_batch": ["52", 0]` to BOTH `VHS_LoadVideoFFmpeg.inputs` AND `VHS_VideoCombine.inputs`. VHS uses the manager as the coordination handle across sub-executions.
- Everything else in the graph stays as if it were a still-image workflow.

Reference: `server/workflows/krea2_turbo_local_anim.json`.

### Runner cascade handling — DO NOT "FIX" THIS

`server/modules/_workflow_local.py:submit_and_wait` calls `_await_meta_batch_completion` when it detects `unfinished_batch: [true]` in any node's history entry. That helper:

1. Polls `/queue` until both `queue_pending` and `queue_running` are empty for 2 consecutive checks (avoids the race where one sub-execution finishes and the next hasn't been queued yet).
2. Fetches `/history` and walks entries in reverse to find the newest one with real file outputs.

If the runner returns early with `unfinished_batch: true` as the "final" output, the error is `no output matching ['mp4'] in /history. Outputs keys: ['30:20', '29']` — the mp4 hasn't been written yet because the cascade isn't done. Don't rewrite the polling loop; if this signature appears, the BatchManager wiring is likely wrong or the ComfyUI queue isn't draining.

## Scene-video input specifics

`scene-video` input specs auto-populate `kwargs["video_path"]` on the backend from `_video_store[node_id]` (transport recording) or the scene bg video as fallback. The local runner then calls `upload_video_to_local` and passes the ComfyUI-side filename to whichever `widget_name` the patch targets. Wire scene-video patches to VHS's `video` widget (the upload-variant enum) — that's what `apply_manifest_inputs` writes to. Never target a Path-variant node; the runner has no local-path passthrough.

Frontend rendering: the scene-video slot uses the same `.gen-slot` markup as scene-image (upload / drop / clear). Wiring is in `renderWorkflowCellProperties`'s `.wf-scene-video` loop. Uploaded videos land in `_video_store[NODE_ID]` (via `/comfyblockout/save_video`), replacing whatever recording was there. This is intentional and expected — the scene-video slot is a "video reference" whether it came from the transport ● button or a drop.

## Properties-pane UI patterns

The workflow cell's LEFT sidebar row is a compact tile (icon + label + run/× buttons); every real input control renders in the right-hand PROPERTIES pane via `renderWorkflowCellProperties`. It reads `manifest.inputs` in order and dispatches per `spec.type`. Match these types + their patch shapes and the pane renders correctly with zero extra frontend code:

- **`textarea`** — big multiline prompt input. Placeholder from `spec.placeholder`. Use for user prompts, negative prompts, any free-form text.
- **`text`** — single-line string input.
- **`number`** — `.wf-drag` chip with click-and-drag numeric scrubbing. Fields: `default`, `min`, `max`, `step`. Coerces to int (whole numbers) or float on submit. Use for cfg, denoise, strength, steps, fps, any per-run knob that changes value.
- **`seed`** — `.wf-drag` chip PLUS a 🎲/🔒 mode button. Random by default (a fresh seed per run). User clicks 🔒 to lock the current seed so re-runs are deterministic. Coerces to int. The mode is persisted per-cell as `<seedname>_mode`.
- **`dropdown`** — combo picker. Options from either `spec.options` (baked list) OR `spec.options_source: { class_type, widget }` (live-fetched from ComfyUI's `/object_info` at render time — preferred for sampler/scheduler/model-file dropdowns).
- **`scene-image`** — `.gen-slot` upload widget. Empty = runner uses the current viewport snapshot; user click / drop replaces with a specific image. The runner uploads the image to ComfyUI's `input/` dir and patches the returned filename into `widget_name` (typically `"image"` on a LoadImage node).
- **`scene-video`** — same `.gen-slot` UX, videos only. Empty = runner uses the transport recording (● button) or scene bg video as fallback. Drop / upload replaces the recording for this session.

Every input can carry a `label` (falls back to the humanized `name`). Order in `manifest.inputs` is the render order in the pane.

## Blockout image / video: the full pipeline

The user-facing "blockout" is the viewport render — the 3D scene the user is composing. It's the primary reference every workflow sees. Two flavors that share the same mental model:

### Image blockout (scene-image path)

1. User opens the Blockout tab OR clicks Generate on any workflow cell with a `scene-image` input.
2. `autoSnapshot` in `web/editor.html` renders one frame of the scene through the same pipeline the animate loop uses (renderCam, full lighting, SMAA — byte-identical to a live viewport frame), captures the canvas as PNG, and POSTs it to `/comfyblockout/save_image?node_id=<NODE_ID>`.
3. Server stashes it in `_image_store[NODE_ID]` (single-slot per node).
4. When any workflow with a `scene-image` input runs, main.py resolves `image_path = _image_store[NODE_ID]["path"]` and passes it to the module runner.
5. Cloud runner: `upload_image_to_cloud` → filename patched into LoadImage. Local runner: `upload_image_to_local` → same.
6. If the user drops / uploads a custom image on the workflow cell's slot, the frontend POSTs it to the same `/comfyblockout/save_image` endpoint — overwriting the auto-snapshot. So the "custom image" path is just "different bytes in the same slot."

Race gotcha (documented in `feedback_image_store_race`): opening the Blockout tab triggers an autoSnapshot that CLOBBERS a user's uploaded image. The frontend re-uploads the cached user blob just before running to prevent this — don't remove that re-upload without understanding the race.

### Video blockout (scene-video path)

1. User hits ● on the transport (bottom of the viewport). Recording captures the canvas via `MediaRecorder` at ~30fps. On stop, the blob is POSTed to `/comfyblockout/save_video` and stored in `_video_store[NODE_ID]`.
2. When a workflow with a `scene-video` input runs, main.py resolves `video_path = _video_store[NODE_ID]["path"]`.
3. Local runner uploads to ComfyUI's `input/` dir via `upload_video_to_local` and patches the returned filename into the LoadVideo widget (VHS_LoadVideoFFmpeg or similar).
4. Same custom-drop behavior as scene-image: dropping a video on the cell's slot POSTs to `/comfyblockout/save_video`, replacing the recording.

Both paths preserve one universal invariant: **the workflow module never knows whether its input is a viewport auto-capture, a transport recording, or a user upload.** The runner exposes a single `image_path` / `video_path` kwarg. Everything upstream is just "where did those bytes come from."

## Output display

The workflow returns `{path, filename, ext}` from `run()`. Main.py serves the file at `/output/<subfolder>/<filename>` (subfolder partitioned by extension via `_EXT_KIND`: images/videos/3d/audio). Frontend picks up the URL and drives three surfaces:

1. **Result overlay** (`#result-overlay` — the AR rectangle over the viewport). `applyResultMode("render")` sets `resultOverlayImg.src` (still) or `resultOverlayVid.src` (video, autoplay/loop/muted) based on the result's file extension. `lastResult.kind` is `"video"` if `ext in {mp4, webm, mov}`, else `"image"`.
2. **Compare slider** (drag-to-wipe handle between blockout and render). `syncCompareMode` picks the underlay:
   - Video-kind render → shows the recorded blockout VIDEO (`#result-overlay-blockout-vid`, synced via `.blockout-is-video` class).
   - Image-kind render → shows the still blockout snapshot (`#result-overlay-blockout-img`).
   - The `<video>` element for blockout is auto-play/loop/muted so the underlay plays while the user drags.
3. **Output pane** (`.assets-tile` list on the right, RECENT / IMAGES / VIDEOS / 3D / BLOCKOUT tabs). Files land in `data/output/` on save, glob'd by extension. New outputs written by module runs auto-hydrate the pane on next open.

For video-mode workflows, the result-overlay's × clear button (`#result-clear-overlay`) is wired to `/comfyblockout/video/<node_id>` DELETE — clears the recorded video so the Blockout tab falls back to the still snapshot.

## Full working example: `krea2_turbo_local_anim`

Everything above coheres into a real workflow. Study these files as the reference implementation for a video-in/video-out local module:

- `server/workflows/krea2_turbo_local_anim.json` — the graph. `VHS_LoadVideoFFmpeg` (upload variant) → `VAEEncode` → `KSampler` → `VAEDecode` → `VHS_VideoCombine`. `VHS_BatchManager` wired to both LoadVideo and VideoCombine's `meta_batch` inputs for per-frame processing. Prompt refinement (`TextGenerate` + `PreviewAny`) preserved from the still i2i template.
- `server/workflows/krea2_turbo_local_anim.meta.json` — the manifest. Demonstrates: scene-video input, multi-target patch (`fps` mirrors to both LoadVideo.force_rate and VideoCombine.frame_rate via `patch: [...]`), `options_source` dropdowns for sampler/scheduler, `type: "seed"` seed, `type: "number"` denoise/cfg/steps.
- `server/modules/_workflow_local.py:submit_and_wait` — the runner path with meta-batch cascade awareness.
