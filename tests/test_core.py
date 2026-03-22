import unittest

from bookfm.analysis import normalize_plan
from bookfm.chunking import chunk_document
from bookfm.models import Document


class ChunkingTests(unittest.TestCase):
    def test_chunk_document_respects_headings_and_limits(self) -> None:
        text = (
            "Chapter 1\n\n"
            "One two three four five six seven eight nine ten.\n\n"
            "Eleven twelve thirteen fourteen fifteen sixteen.\n\n"
            "Chapter 2\n\n"
            "Alpha beta gamma delta epsilon zeta eta theta."
        )
        document = Document(source_type="text", source_name="inline", title="Demo", full_text=text)
        chunked = chunk_document(
            document,
            reading_speed_wpm=200,
            target_seconds=6,
            min_seconds=1,
            max_seconds=10,
            max_chars=200,
        )

        self.assertGreaterEqual(len(chunked.sections), 1)
        self.assertEqual(chunked.sections[0].title, "Section 1")

    def test_chunk_document_splits_dense_text_by_sentences(self) -> None:
        text = (
            "This is sentence one. This is sentence two with more words. "
            "This is sentence three and it keeps going a bit longer. "
            "This is sentence four. This is sentence five."
        )
        document = Document(source_type="text", source_name="inline", title="Dense", full_text=text)
        chunked = chunk_document(
            document,
            reading_speed_wpm=120,
            target_seconds=4,
            min_seconds=1,
            max_seconds=5,
            max_chars=80,
        )

        self.assertGreaterEqual(len(chunked.sections), 2)
        self.assertTrue(all(section.paragraph_count >= 1 for section in chunked.sections))


class AnalysisTests(unittest.TestCase):
    def test_normalize_plan_clamps_values(self) -> None:
        plan = normalize_plan(
            {
                "composer_prompt": "soft piano",
                "mood_tags": ["calm"],
                "genre_tags": ["ambient"],
                "instruments": ["piano"],
                "bpm": 999,
                "density": 5,
                "brightness": -1,
                "guidance": 9,
                "temperature": -2,
            }
        )

        self.assertEqual(plan.composer_prompt, "soft piano")
        self.assertEqual(plan.bpm, 200)
        self.assertEqual(plan.density, 1.0)
        self.assertEqual(plan.brightness, 0.0)
        self.assertEqual(plan.guidance, 6.0)
        self.assertEqual(plan.temperature, 0.0)



if __name__ == "__main__":
    unittest.main()
