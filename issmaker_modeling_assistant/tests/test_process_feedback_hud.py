"""Shared Process HUD safety, layout, stage and cancellation protocol."""
import unittest
from issmaker_modeling_assistant.core import process_feedback as process


class ProcessFeedbackContract(unittest.TestCase):
    def tearDown(self):
        process.reset()

    def test_monotone_confirmed_stages(self):
        token=process.begin("MODEL_TASK","Построение",("Старт",))
        process.configure(token,("Анализ","LP","HP","Интерьер","Контроль","Финал"))
        self.assertEqual(process.stage(token,2,"LP")["fraction"] if "fraction" in process.stage(token,2) else process.current().fraction,2/6)
        self.assertRaisesRegex(ValueError,"MONOTONIC",process.stage,token,1,"Анализ")
        self.assertEqual(process.stage(token,4,"Интерьер")["completed"],4)
        self.assertEqual(process.finish(token,"Готово")["state"],"SUCCESS")
        self.assertEqual(process.snapshot()["completed"],6)

    def test_invalid_progress_does_not_fake_success(self):
        token=process.begin("WALL","Стена",("Сетка","Готово"))
        with self.assertRaises(ValueError):process.stage(token,3)
        process.fail(token,"Сбой геометрии")
        self.assertEqual(process.snapshot()["state"],"FAILED")
        self.assertEqual(process.snapshot()["completed"],0)

    def test_busy_token_cannot_interleave_tools(self):
        a=process.begin("MODEL_TASK","Первый",("Анализ",))
        with self.assertRaises(RuntimeError):process.begin("WALL","Второй",("Анализ",))
        with self.assertRaises(RuntimeError):process.stage(a+1,0)
        self.assertEqual(process.cancel(a)["state"],"CANCELLED")

    def test_responsive_layout(self):
        for size in ((1920,1080),(1426,952),(900,550),(460,350),(300,260)):
            x,y,w,h,s=process.layout(*size)
            self.assertGreaterEqual(x,0)
            self.assertGreaterEqual(y,0)
            self.assertLessEqual(x+w,size[0]+.01)
            self.assertLessEqual(y+h,size[1]+.01)

    def test_large_batch_progress_has_no_false_percentage(self):
        token=process.begin("MODEL_TASK","100 операций",("План",))
        process.configure(token,tuple(str(i) for i in range(402)))
        self.assertEqual(process.stage(token,11)["completed"],11)
        self.assertAlmostEqual(process.current().fraction,11/402)
        process.cancel(token)
        self.assertEqual(process.snapshot()["completed"],11)
        self.assertLess(process.snapshot()["completed"],402)

if __name__=="__main__":
    unittest.main()
