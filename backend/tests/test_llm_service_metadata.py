"""UsageAwareLLM metadata API remains additive to the legacy two-value call."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.llm_service import LLMCompletion, UsageAwareLLM


class UsageAwareLLMMetadataTests(unittest.TestCase):
    def test_metadata_exposes_finish_reason_and_legacy_usage_shape_is_unchanged(self):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        completion = LLMCompletion(text="{}", usage=usage, finish_reason="stop")
        llm = object.__new__(UsageAwareLLM)

        with patch.object(UsageAwareLLM, "_invoke_completion", return_value=completion):
            metadata = llm.invoke_with_metadata([{"role": "user", "content": "test"}])
            legacy = llm.invoke_with_usage([{"role": "user", "content": "test"}])

        self.assertEqual(metadata, completion)
        self.assertEqual(legacy, ("{}", usage))
        self.assertEqual(len(legacy), 2)


if __name__ == "__main__":
    unittest.main()
