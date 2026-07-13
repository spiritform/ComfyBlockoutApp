# AnimoFlow — text-to-motion

Local MoMask container synthesizes a motion clip from a text description, then retargets it onto an AF_Mannequin in the scene (spawns one if none exist).

## Tool

`run_animoflow({prompt, max_frames?, seed?})`

## Requirements

- Docker Desktop running
- AnimoFlow containers up

Setup lives in the Motion tool pane — **point the user there if the call fails with an env error.**

## Good prompts

Short verb phrases:

- "person walking forward"
- "a character waving"
- "kick with the right leg then step back"

## Frame count

- 30–240 typical
- 20 fps → 120 frames = 6 seconds

## Warning

The first run of the day can take 30–90s of CPU inference. Warn the user upfront so they don't think the call hung.
