#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from agents.vikunja_adapter import Client, FLOW_DOC_URL, MAX_COMMENT_CHARS, MAX_COMMENT_WORDS, brief_comment, description, load_task, token_from


class VikunjaAdapterTest(unittest.TestCase):
    def test_ticket_comments_are_brief_and_link_flow_documentation(self):
        comment = brief_comment("palabra " * 80)

        self.assertLess(len(comment.split()), MAX_COMMENT_WORDS + 1)
        self.assertIn(f"[agents/FLOW.md]({FLOW_DOC_URL})", comment)
        self.assertTrue(comment.endswith(f"[agents/FLOW.md]({FLOW_DOC_URL})"))

    def test_brief_comment_preserves_markdown_newlines_and_code_blocks(self):
        text = "Línea 1\n\n```\nkey: value\n```\n\n👉 Respondé `/tomar` o `/ignorar`."
        comment = brief_comment(text)
        self.assertIn("\n\n```\nkey: value\n```\n\n", comment)
        self.assertIn("👉 Respondé `/tomar` o `/ignorar`.", comment)
        self.assertLess(len(comment.split()), MAX_COMMENT_WORDS + 1)
        self.assertIn(f"[agents/FLOW.md]({FLOW_DOC_URL})", comment)

    def test_brief_comment_preserves_existing_specific_links(self):
        specific = "Me asignaron este ticket.\n\n[Guía](https://example.com/doc#seccion)"
        comment = brief_comment(specific)
        self.assertEqual(comment, specific)
        self.assertNotIn(FLOW_DOC_URL, comment)

    def test_brief_comment_protects_trailing_links_when_truncating_long_body(self):
        long_body = "palabra " * 70 + "\n\n[Evidencia](.runtime/runs/1) | [Doc](https://example.com/doc)"
        comment = brief_comment(long_body)
        self.assertLess(len(comment.split()), MAX_COMMENT_WORDS + 1)
        self.assertTrue(comment.endswith("[Doc](https://example.com/doc)"))
        self.assertNotIn("](.runtime/", comment)
        self.assertIn("…", comment)

    def test_brief_comment_has_hard_character_limit_and_keeps_midstream_doc_link(self):
        comment = brief_comment("A" * 20_000 + " [Doc](https://example.com/doc) " + "B" * 20_000)
        self.assertLessEqual(len(comment), MAX_COMMENT_CHARS)
        self.assertLess(len(comment.split()), MAX_COMMENT_WORDS + 1)
        self.assertIn("[Doc](https://example.com/doc)", comment)

    def test_component_doc_link(self):
        from agents.vikunja_adapter import component_doc_link, REPO_URL
        here = Path(__file__).resolve().parent
        link = component_doc_link(here.parent, "agents")
        self.assertEqual(link, f"[agents/README.md]({REPO_URL}/blob/main/agents/README.md)")
        link_tree = component_doc_link(here.parent, "scripts")
        self.assertEqual(link_tree, f"[Flujo de agentes]({FLOW_DOC_URL})")

    def test_description_is_structured_and_secret_free(self):
        value = description({"id": "VIK-1", "title": "POC", "component": "agents", "type": "code", "risk": "medium", "protected_actions": ["cron"]})
        self.assertIn("`VIK-1`", value)
        self.assertIn("`medium`", value)
        self.assertNotIn("tk_", value)

    def test_token_requires_vikunja_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.env"
            path.write_text("VIKUNJA_API_TOKEN=tk_valid_token_123\n")
            self.assertEqual(token_from(path), "tk_valid_token_123")
            path.write_text("VIKUNJA_API_TOKEN=invalid\n")
            with self.assertRaises(ValueError):
                token_from(path)

    def test_load_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"tasks": {"VIK-1": {"id": "VIK-1"}}}))
            self.assertEqual(load_task(path, "VIK-1")["id"], "VIK-1")

    def test_markdown_format_is_part_of_the_api_path(self):
        client = Client("http://vikunja.test/api/v2", "tk_test")
        self.assertEqual(client.base_url + "/tasks/1?format=markdown", "http://vikunja.test/api/v2/tasks/1?format=markdown")


if __name__ == "__main__":
    unittest.main()
