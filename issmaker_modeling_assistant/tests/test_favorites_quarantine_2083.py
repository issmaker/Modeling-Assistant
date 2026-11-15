import unittest
from issmaker_modeling_assistant.core.quick_favorites import validate,SCHEMA


class FavoritesStrictValidationTests(unittest.TestCase):
    def test_unknown_id_is_rejected_not_silently_discarded(self):
        before={"schema":SCHEMA,"favorites":["COLLISION","UNEXPECTED_783"],"last_shading":None}
        with self.assertRaisesRegex(ValueError,"INVALID_FAVORITE_IDS"):
            validate(before)
        self.assertEqual(before["favorites"],["COLLISION","UNEXPECTED_783"])

    def test_valid_favorites_preserved(self):
        before={"schema":SCHEMA,"favorites":["UV_WALL","COLLISION"],"last_shading":None}
        self.assertEqual(validate(before)["favorites"],before["favorites"])


if __name__=="__main__": unittest.main()
