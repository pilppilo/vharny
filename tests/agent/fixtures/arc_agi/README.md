# ARC-AGI-3 conformance fixtures

Recorded from the official ARC-AGI Toolkit for the ARC-AGI-3 adapter
(Linear COD-34). Tests read them with no network and no API key.

## `dc22-random-seed0.json`

- **Game:** `dc22-fdcac232` (public, tag `keyboard_click`; legal actions
  1, 2, 3, 4 and 6, so both simple and click actions are covered).
- **Toolkit:** `arc-agi` 0.9.10, `arcengine` 0.9.3, local mode (game downloaded
  from the API, run on this machine).
- **Policy:** `random.Random(0)`, uniform over `action_space`; `ACTION6`
  coordinates uniform in 0–63. It ends in `GAME_OVER` after 127 steps.
- **Contents:** the reset frame, frames for the first six steps and the last
  step, the full action trace, and the scorecard with its `api_key` removed.
  Each grid row is 64 hex digits, one per cell (colours 0–15).
- **Not covered:** a level completion. No public game completed a level under
  random play in 400 steps, and a solved trace would be game-specific.

## Toolkit interface verified against the pinned version

- `arc_agi.Arcade(operation_mode=..., environments_dir="environment_files", arc_base_url=...)`.
  Modes: `normal` (local and API), `online` (API only), `offline` (local only)
  and `competition`. `OPERATION_MODE`, `ENVIRONMENTS_DIR`, `ARC_API_KEY` and
  `ARC_BASE_URL` override them. Without a key it fetches an anonymous one.
- **Base URL:** 0.9.10 defaults to `https://arcprize.org`; the ARC-AGI-3 API is
  at `https://three.arcprize.org` (as the docs state), so pass it explicitly.
- `Arcade.make(game_id, seed=0, scorecard_id=None, save_recording=False, include_frame_data=True, ...)`
  returns an environment or `None`. In `normal` mode it downloads the game into
  `environment_files/<id>/<version>/` (a Python game file and `metadata.json`
  with `baseline_actions` per level).
- `env.reset()` and `env.step(action, data=None, reasoning=None)` return
  `arcengine.FrameDataRaw` or `None`. `env.action_space` is a
  `list[GameAction]`; `env.observation_space` is the last frame; `env.info` is
  `EnvironmentInfo` (`game_id`, `title`, `tags`, ...).
- `FrameDataRaw` fields: `game_id`, `state`, `levels_completed`, `win_levels`,
  `action_input` (`id`, `data`, `reasoning`), `guid`, `full_reset`,
  `available_actions` (`list[int]`). The grid is the private attribute
  `frame`: a list of 64×64 numpy arrays, not serialized by `model_dump`.
- `GameAction`: `RESET`=0 and `ACTION1`–`ACTION7`. Only `ACTION6` is complex and
  takes `data={"x": 0-63, "y": 0-63}` (`ComplexAction`).
- `GameState`: `NOT_PLAYED`, `NOT_FINISHED`, `WIN`, `GAME_OVER`.
- Scorecards: `open_scorecard()` / `create_scorecard()` return an id;
  `get_scorecard(id=None)` and `close_scorecard(id=None)` return an
  `EnvironmentScorecard` with per-environment runs, per-level scores, level
  actions and baseline actions. A default scorecard is opened on first `make`.

## Licences

The toolkit (`arc-agi`), the engine (`arcengine`) and the downloaded game files
are all MIT licensed, © 2026 ARC Prize Foundation.

## Re-recording

After changing the pinned toolkit version, with the `arc` extra installed and
network access to `three.arcprize.org`:

```text
python tools/record_arc_fixtures.py dc22-fdcac232 \
    tests/agent/fixtures/arc_agi/dc22-random-seed0.json --workdir /tmp/arc-work
```

`test_fixture_conforms_to_the_installed_toolkit` fails when the installed
toolkit version differs from the recorded one.
