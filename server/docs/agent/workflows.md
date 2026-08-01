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

## Scene-image input (viewport snapshot as workflow input)

When a workflow has a `scene-image` input, the user cell shows an empty slot with the hint "Empty = uses current viewport". Empty slot → the runner captures the current 3D viewport as the input image. Uploaded/dragged image → that reference image is used instead.

- User asks "why is my workflow using the scene instead of the image I picked?" → check that the cell.values for the image key is set (they may have clicked Generate before the upload finished, or the upload failed silently).
- User asks "how do I feed a reference image?" → tell them to click/drop onto the workflow cell's image slot.

## When you build a workflow (not just import), wire scene-image correctly

**(a)** INCLUDE a LoadImage node in the graph — even if the source model has its own image input, always route through LoadImage so the runner's viewport-snapshot / upload-reference flow resolves cleanly.

**(b)** The manifest's scene-image patch MUST target the LoadImage node's `image` widget (API format: `widget_name: "image"`, node_id = LoadImage's id). NEVER target the compute node's image socket directly — that expects a decoded IMAGE tensor, and the runner only knows how to upload a filename to LoadImage's widget.

**(c)** In API format the compute node's `image` input should be a link reference like `["<loadimage_id>", 0]` — output index 0 of LoadImage is IMAGE, index 1 is MASK.

**(d)** In graph format the same wiring goes through the top-level `links[]` array as `[<link_id>, <loadimage_id>, 0, <compute_id>, <input_slot>, "IMAGE"]`.

If any part of this wiring is off, the compute node receives a filename string instead of a tensor and errors with `'str' object has no attribute 'shape'` at runtime.

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
