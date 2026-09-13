import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_progress.py"
SPEC = importlib.util.spec_from_file_location("pr_progress", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

ROOT = Path(__file__).parents[1]


class RenderTests(unittest.TestCase):
    def state(self, **overrides):
        state = MODULE.blank_state()
        state.update(overrides)
        return state

    def test_running_body_carries_marker_and_phase_table(self):
        state = self.state(phases={"prepare": "ok", "agent": "running",
                                   "verify": "pending", "publish": "pending"})
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}):
            body = MODULE.render(state)
        self.assertIn("<!-- ai-lean-generate:progress:42:1 -->", body)
        self.assertIn("| Prepare task | ✅ done |", body)
        self.assertIn("| Coding agent | 🔄 running |", body)
        self.assertIn("Working on this pull request", body)

    def test_task_list_renders_checkboxes_and_progress_count(self):
        todos = [
            {"content": "Read the diff", "status": "completed"},
            {"content": "Prove the lemma", "status": "in_progress"},
            {"content": "Run lake build", "status": "pending"},
        ]
        body = MODULE.render(self.state(todos=todos))
        self.assertIn("**Agent task list** (1/3 complete)", body)
        self.assertIn("- [x] Read the diff", body)
        self.assertIn("- [ ] Prove the lemma *(in progress)*", body)

    def test_failed_phase_changes_the_headline(self):
        body = MODULE.render(self.state(phases={"prepare": "ok", "agent": "fail",
                                                "verify": "skip", "publish": "skip"}))
        self.assertIn("❌ **Failed.**", body)

    def test_detail_pipes_do_not_break_the_table(self):
        state = self.state(details={"verify": "lake env lean | tail"})
        row = [line for line in MODULE.render(state).splitlines()
               if line.startswith("| Independent verification")][0]
        self.assertIn(r"lake env lean \| tail", row)
        self.assertEqual(len(row.split(" | ")), 3)


class StateRoundTripTests(unittest.TestCase):
    def test_rendered_body_restores_state_in_a_later_job(self):
        state = MODULE.blank_state()
        state["phases"]["agent"] = "ok"
        state["details"]["agent"] = "agent finished"
        state["todos"] = [{"content": "Prove it", "status": "completed"}]
        state["context"] = "Provider `claude-code`, head `abc1234`."
        body = MODULE.render(state)

        restored = MODULE.parse_body(body)
        self.assertEqual(restored["phases"]["agent"], "ok")
        self.assertEqual(restored["details"]["agent"], "agent finished")
        self.assertEqual(restored["todos"][0]["content"], "Prove it")
        self.assertEqual(restored["context"], state["context"])

    def test_detail_cannot_close_the_hidden_state_block(self):
        state = MODULE.blank_state()
        state["details"]["verify"] = "arrow --> here"
        restored = MODULE.parse_body(MODULE.render(state))
        self.assertEqual(restored["details"]["verify"], "arrow --> here")

    def test_a_concurrent_reader_never_sees_a_half_written_state(self):
        """The watcher and the phase setters share the file; a torn read would
        rerender the comment with every phase reset to queued."""
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / ".ai-lean-generate"
            with patch.object(MODULE, "WORK", work), \
                    patch.object(MODULE, "STATE", work / "progress.json"):
                state = MODULE.blank_state()
                state["phases"] = {"prepare": "ok", "agent": "ok",
                                   "verify": "ok", "publish": "running"}
                MODULE.save_state(state)
                # Only the final state file is ever left behind: no partial
                # temp file is mistaken for it, and the rename is atomic.
                self.assertEqual([p.name for p in sorted(work.iterdir())],
                                 ["progress.json"])
                self.assertEqual(MODULE.load_state()["phases"], state["phases"])

    def test_an_unreadable_state_file_is_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / ".ai-lean-generate"
            work.mkdir()
            state_path = work / "progress.json"
            state_path.write_text('{"phases": {"prepare": "ok"', encoding="utf-8")
            with patch.object(MODULE, "WORK", work), \
                    patch.object(MODULE, "STATE", state_path):
                # None tells command_set to rebuild from the comment body
                # instead of silently resetting the table to a blank state.
                self.assertIsNone(MODULE.read_state_file())
                self.assertEqual(MODULE.load_state()["phases"],
                                 {key: "pending" for key, _ in MODULE.PHASES})

    def test_missing_state_block_yields_a_blank_state(self):
        self.assertEqual(MODULE.parse_body("just a comment"), MODULE.blank_state())


class TodoExtractionTests(unittest.TestCase):
    def message(self, todos):
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": todos},
                    }
                ]
            },
        }

    def test_last_todo_write_wins(self):
        objects = [
            self.message([{"content": "first", "status": "pending"}]),
            {"type": "user", "message": {"content": "noise"}},
            self.message([{"content": "second", "status": "completed"}]),
        ]
        self.assertEqual(
            MODULE.todos_from_objects(objects),
            [{"content": "second", "status": "completed"}],
        )

    def test_other_tools_are_ignored(self):
        objects = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"todos": []}}
                    ]
                },
            }
        ]
        self.assertIsNone(MODULE.todos_from_objects(objects))

    def test_live_transcript_is_read_despite_a_torn_final_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / ".claude" / "projects" / "repo"
            projects.mkdir(parents=True)
            transcript = projects / "session.jsonl"
            transcript.write_text(
                json.dumps(self.message([{"content": "live", "status": "in_progress"}]))
                + "\n{\"type\": \"assis",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AI_LEAN_SANDBOX_HOME": tmp, "HOME": tmp}):
                self.assertEqual(
                    MODULE.todos_from_objects(MODULE.read_jsonl(transcript)),
                    [{"content": "live", "status": "in_progress"}],
                )


class WiringTests(unittest.TestCase):
    def test_todo_write_is_granted_only_when_a_list_is_asked_for(self):
        # Granted unconditionally the agent uses it anyway, and every call
        # costs a turn against --max-turns. That overran the budget on a run
        # where reporting was off and nothing was reading the list.
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        step = next(
            s for s in action["runs"]["steps"]
            if s.get("id") == "claude-agent"
        )
        args = step["with"]["claude_args"]
        self.assertIn("TodoWrite", args)
        self.assertIn("steps.prepare-agent.outputs.progress-tasks == 'true'", args)

    def test_the_todo_write_grant_tracks_the_prompt_instruction(self):
        spec = importlib.util.spec_from_file_location(
            "prepare_agent", ROOT / "scripts" / "prepare_agent.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value, wanted in (("true", True), ("false", False), ("", False)):
            with patch.dict(os.environ, {"AI_LEAN_PROGRESS_TASKS": value}):
                self.assertEqual(module.progress_tasks_enabled(), wanted)
                self.assertEqual(bool(module.progress_block()), wanted)

    def test_progress_phases_match_the_helper_action_documentation(self):
        helper = yaml.safe_load((ROOT / "progress" / "action.yml").read_text(encoding="utf-8"))
        described = helper["inputs"]["phase"]["description"]
        for key, _ in MODULE.PHASES:
            self.assertIn(key, described)

    def test_progress_steps_never_receive_the_agent_token_path(self):
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        agent = next(s for s in action["runs"]["steps"] if s.get("id") == "claude-agent")
        self.assertEqual(agent["env"]["GITHUB_TOKEN"], "")
        self.assertEqual(agent["env"]["GH_TOKEN"], "")
        self.assertNotIn("AI_LEAN_PROGRESS_TOKEN", agent["env"])


class PromptTests(unittest.TestCase):
    def test_task_list_instruction_only_appears_when_reporting_is_on(self):
        spec = importlib.util.spec_from_file_location(
            "prepare_agent", ROOT / "scripts" / "prepare_agent.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {"AI_LEAN_PROGRESS_TASKS": "true"}):
            self.assertIn("TodoWrite", module.progress_block())
        with patch.dict(os.environ, {"AI_LEAN_PROGRESS_TASKS": "false"}):
            self.assertEqual(module.progress_block(), "")


if __name__ == "__main__":
    unittest.main()
